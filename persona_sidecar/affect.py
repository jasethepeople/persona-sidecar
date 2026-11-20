import hashlib
import json
import math
import os
import threading
import uuid
from pathlib import Path

from .config import DIMENSIONS, anchor_for
from .database import append_audit, connect, transaction

LABEL_DELTAS = {
    "affectionate": {"elation": 0.10, "longing": 0.06, "play": 0.05, "irritability": -0.06, "dejection": -0.05},
    "playful": {"play": 0.18, "elation": 0.10, "irritability": -0.04},
    "reassuring": {"dejection": -0.12, "irritability": -0.10, "longing": -0.05, "elation": 0.06},
    "vulnerable": {"longing": 0.12, "dejection": 0.10, "fatigue": 0.04},
    "conflict": {"irritability": 0.18, "dejection": 0.10, "play": -0.12, "elation": -0.10},
    "hostile": {"irritability": 0.20, "dejection": 0.18, "play": -0.12},
    "struggling": {"fatigue": 0.18, "dejection": 0.10, "elation": -0.06},
    "distant": {"longing": 0.10, "dejection": 0.08, "irritability": 0.04},
    "neutral": {},
}

NEGATIVE_DIMS = frozenset(("irritability", "fatigue", "dejection"))
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(persona_id):
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(persona_id, threading.RLock())


def state_file_path(config, persona_id):
    root = Path(config["state"]["directory"]).expanduser().resolve()
    digest = hashlib.sha256(str(persona_id).encode("utf-8")).hexdigest()
    return root / f"persona-{digest}.json"


def _clamp(value):
    return min(1.0, max(0.0, float(value)))


def initial_state(anchor, now):
    values = {name: _clamp(anchor[name]) for name in DIMENSIONS}
    return {"base": dict(values), "mood": dict(values), "recent_labels": [], "updated_at": float(now)}


def advance_state(state, anchor, now, affect_settings):
    now = float(now)
    previous = float(state.get("updated_at", now))
    elapsed_hours = max(0.0, now - previous) / 3600.0
    if elapsed_hours == 0:
        state["updated_at"] = max(previous, now)
        return state

    base_tau = affect_settings["base_tau_hours"]
    follow_tau = max(0.001, float(affect_settings["mood_follow_tau_hours"]))
    return_tau = max(0.001, float(affect_settings["mood_return_tau_hours"]))
    sleep_gap = float(affect_settings["sleep_gap_hours"])
    sleep_multiplier = max(1.0, float(affect_settings["mood_sleep_return_multiplier"]))
    gain = float(affect_settings["mood_consolidation_gain"])
    min_gain = float(affect_settings["mood_consolidation_min"])
    max_gain = float(affect_settings["mood_consolidation_max"])
    negative_multiplier = max(1.0, float(affect_settings["negativity_tau_multiplier"]))
    is_sleep_gap = elapsed_hours >= sleep_gap

    for name in DIMENSIONS:
        current_base = _clamp(state.get("base", {}).get(name, anchor[name]))
        current_mood = _clamp(state.get("mood", {}).get(name, anchor[name]))
        dimension_tau = max(0.001, float(base_tau.get(name, 8.0)))
        if name in NEGATIVE_DIMS:
            dimension_tau *= negative_multiplier
        current_base = current_mood + (current_base - current_mood) * math.exp(-elapsed_hours / dimension_tau)
        dimension_return_tau = return_tau / (sleep_multiplier if is_sleep_gap else 1.0)
        if name in NEGATIVE_DIMS:
            dimension_return_tau *= negative_multiplier
        neutral = _clamp(anchor[name])
        current_mood = neutral + (current_mood - neutral) * math.exp(-elapsed_hours / dimension_return_tau)
        deviation = abs(current_base - current_mood)
        speed = min(max_gain, max(min_gain, gain * deviation))
        follow = 1.0 - math.exp(-elapsed_hours * speed / follow_tau)
        current_mood += (current_base - current_mood) * follow
        state["base"][name] = _clamp(current_base)
        state["mood"][name] = _clamp(current_mood)

    state["recent_labels"] = [
        item for item in state.get("recent_labels", [])
        if now - float(item.get("ts", -1)) < 86_400
    ][-32:]
    state["updated_at"] = now
    return state


def habituation_scale(state, label, now, affect_settings):
    window = float(affect_settings["habituation_window_seconds"])
    recent_count = sum(
        1 for item in state.get("recent_labels", [])
        if item.get("label") == label and 0 <= now - float(item.get("ts", -1)) < window
    )
    factor = float(affect_settings["habituation_factor"])
    return factor ** recent_count, recent_count


def _read_persona_state(config, persona_id, now):
    path = state_file_path(config, persona_id)
    try:
        with path.open("r", encoding="utf-8") as handle:
            document = json.load(handle)
    except FileNotFoundError:
        return initial_state(anchor_for(config, persona_id), now)
    if not isinstance(document, dict) or document.get("persona_id") != persona_id or not isinstance(document.get("state"), dict):
        raise ValueError("persona state file is invalid or belongs to another persona")
    state = document["state"]
    anchor = anchor_for(config, persona_id)
    for key in ("base", "mood"):
        values = state.setdefault(key, {})
        for name in DIMENSIONS:
            values[name] = _clamp(values.get(name, anchor[name]))
    state.setdefault("recent_labels", [])
    state["updated_at"] = float(state.get("updated_at", now))
    return advance_state(state, anchor, now, config["affect"])


def _write_persona_state(config, persona_id, state):
    path = state_file_path(config, persona_id)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    document = {"persona_id": persona_id, "state": state}
    encoded = (json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def apply_label_in_transaction(conn, config, persona_id, label, now, arousal=0.5):
    if label not in LABEL_DELTAS:
        return None
    now = float(now)
    arousal = _clamp(arousal)
    anchor = anchor_for(config, persona_id)
    with _lock_for(persona_id):
        state = _read_persona_state(config, persona_id, now)
        scale, prior_same = habituation_scale(state, label, now, config["affect"])
        arousal_scale = 0.35 + 0.65 * arousal
        applied = {}
        for name, nominal in LABEL_DELTAS[label].items():
            delta = float(nominal) * scale * arousal_scale
            value = state["base"][name]
            effective = delta * 2.0 * (1.0 - value) if delta > 0 else delta * 2.0 * value
            state["base"][name] = _clamp(value + effective)
            applied[name] = effective
        state["recent_labels"].append({"label": label, "ts": now})
        state["recent_labels"] = state["recent_labels"][-32:]
        state["updated_at"] = now
        append_audit(conn, {
            "type": "affect.updated", "persona_id": persona_id, "label": label,
            "prior_same_label": prior_same, "habituation_scale": scale,
        }, now)
        _write_persona_state(config, persona_id, state)
        return {"state": state, "deltas": applied, "habituation_scale": scale, "prior_same_label": prior_same}


def apply_label(db_path, config, persona_id, label, now, arousal=0.5):
    conn = connect(db_path)
    try:
        with transaction(conn):
            return apply_label_in_transaction(conn, config, persona_id, label, now, arousal)
    finally:
        conn.close()


def get_state(db_path, config, persona_id, now):
    with _lock_for(persona_id):
        state = _read_persona_state(config, persona_id, float(now))
        _write_persona_state(config, persona_id, state)
        return state


def format_context(state):
    lines = ["[affect]"]
    for name in DIMENSIONS:
        lines.append(f"{name}: {state['base'][name]:.3f} (mood {state['mood'][name]:.3f})")
    lines.append("[/affect]")
    return "\n".join(lines)
