import copy
import ipaddress
import json
import os
from urllib.parse import urlsplit

DIMENSIONS = ("irritability", "fatigue", "elation", "longing", "dejection", "play")
DEFAULT_ANCHOR = {
    "irritability": 0.15,
    "fatigue": 0.20,
    "elation": 0.20,
    "longing": 0.25,
    "dejection": 0.12,
    "play": 0.25,
}
DEFAULT_CONFIG = {
    "server": {"host": "127.0.0.1", "port": 24601, "max_body_bytes": 65536},
    "database": {"path": "./data/persona-sidecar.sqlite3"},
    "state": {"directory": "./data/personas"},
    "gateway": {
        "token_env": "SIDECAR_GATEWAY_TOKEN",
        "requests_per_minute": 60,
        "max_text_chars": 12000,
    },
    "delivery": {
        "adapter": "webhook",
        "url": "http://127.0.0.1:24700/messages",
        "auth_token_env": "",
        "timeout_seconds": 15,
        "max_attempts": 8,
        "poll_seconds": 0.25,
    },
    "timing": {
        "reading_wpm": 220,
        "latency_median_seconds": 1.8,
        "latency_sigma": 0.45,
        "max_burst_chars": 500,
    },
    "damage": {
        "enabled": True,
        "typo_probabilities": {
            "substitution": 0.002,
            "transposition": 0.001,
            "deletion": 0.001,
            "insertion": 0.001,
        },
        "lowercase_start_probability": 0.02,
        "drop_terminal_punctuation_probability": 0.015,
        "self_correction_probability": 0.04,
    },
    "egress": {
        "mode": "direct",
        "proxy_url": "",
        "proxy_url_env": "",
        "timeout_seconds": 12,
        "ip_observation_url": "https://api.ipify.org",
    },
    "affect": {
        "anchor": DEFAULT_ANCHOR,
        "base_tau_hours": {
            "irritability": 3,
            "fatigue": 8,
            "elation": 3,
            "longing": 8,
            "dejection": 8,
            "play": 3,
        },
        "mood_follow_tau_hours": 12,
        "mood_return_tau_hours": 72,
        "mood_sleep_return_multiplier": 3,
        "sleep_gap_hours": 12,
        "mood_consolidation_gain": 4,
        "mood_consolidation_min": 0.25,
        "mood_consolidation_max": 2.5,
        "negativity_tau_multiplier": 1.6,
        "habituation_window_seconds": 900,
        "habituation_factor": 0.7,
    },
    "classifier": {
        "backend": "rules",
        "endpoint": "http://127.0.0.1:11434/api/chat",
        "model": "qwen2.5:3b",
        "timeout_seconds": 8,
        "poll_seconds": 0.25,
    },
    "personas": {"default": {"anchor": DEFAULT_ANCHOR}},
}
DEFAULTS = DEFAULT_CONFIG


def _merge(base, override):
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def validate_config(config):
    try:
        host = ipaddress.ip_address(config["server"]["host"])
    except ValueError as exc:
        raise ValueError("server.host must be a loopback IP address") from exc
    if not host.is_loopback:
        raise ValueError("server.host must be a loopback IP address")
    if not 1 <= int(config["server"]["port"]) <= 65535:
        raise ValueError("server.port must be between 1 and 65535")
    if int(config["server"]["max_body_bytes"]) < 1024:
        raise ValueError("server.max_body_bytes must be at least 1024")
    if int(config["gateway"]["requests_per_minute"]) < 1:
        raise ValueError("gateway.requests_per_minute must be positive")
    if int(config["gateway"]["max_text_chars"]) < 1:
        raise ValueError("gateway.max_text_chars must be positive")
    if not str(config["state"].get("directory", "")).strip():
        raise ValueError("state.directory must not be empty")
    if config["delivery"]["adapter"] != "webhook":
        raise ValueError("delivery.adapter must be 'webhook'")
    delivery_url = urlsplit(str(config["delivery"]["url"]))
    if delivery_url.scheme not in ("http", "https") or not delivery_url.hostname:
        raise ValueError("delivery.url must be an HTTP or HTTPS URL")
    if float(config["delivery"]["timeout_seconds"]) <= 0:
        raise ValueError("delivery.timeout_seconds must be positive")
    if int(config["delivery"]["max_attempts"]) < 1:
        raise ValueError("delivery.max_attempts must be positive")
    if float(config["timing"]["reading_wpm"]) <= 0:
        raise ValueError("timing.reading_wpm must be positive")
    if float(config["timing"]["latency_median_seconds"]) <= 0:
        raise ValueError("timing.latency_median_seconds must be positive")
    if float(config["timing"]["latency_sigma"]) < 0:
        raise ValueError("timing.latency_sigma must not be negative")
    if int(config["timing"]["max_burst_chars"]) < 20:
        raise ValueError("timing.max_burst_chars must be at least 20")
    egress = config["egress"]
    if egress["mode"] not in ("direct", "http", "socks5"):
        raise ValueError("egress.mode must be direct, http, or socks5")
    if egress["mode"] != "direct":
        proxy_url = str(egress.get("proxy_url", ""))
        proxy_env = str(egress.get("proxy_url_env", ""))
        if proxy_env:
            proxy_url = os.environ.get(proxy_env, "") or proxy_url
        if not proxy_url:
            raise ValueError("egress.proxy_url or egress.proxy_url_env must be configured")
        proxy = urlsplit(proxy_url)
        allowed = ("http",) if egress["mode"] == "http" else ("socks5", "socks5h")
        if proxy.scheme not in allowed or not proxy.hostname or not proxy.port:
            raise ValueError("egress.proxy_url does not match egress.mode")
    classifier = config["classifier"]
    if classifier.get("backend") not in ("rules", "ollama"):
        raise ValueError("classifier.backend must be 'rules' or 'ollama'")
    classifier_url = urlsplit(str(classifier["endpoint"]))
    if classifier_url.scheme not in ("http", "https") or not classifier_url.hostname:
        raise ValueError("classifier.endpoint must be an HTTP or HTTPS URL")
    if not str(classifier.get("model", "")).strip() or float(classifier["timeout_seconds"]) <= 0:
        raise ValueError("classifier.model and a positive timeout_seconds are required")
    for name in DIMENSIONS:
        value = float(config["affect"]["anchor"][name])
        if not 0 <= value <= 1:
            raise ValueError(f"affect.anchor.{name} must be in [0, 1]")
    for persona in config.get("personas", {}).values():
        for name, value in persona.get("anchor", {}).items():
            if name not in DIMENSIONS or not 0 <= float(value) <= 1:
                raise ValueError(f"invalid persona anchor dimension: {name}")
    if not 0 < float(config["affect"]["habituation_factor"]) <= 1:
        raise ValueError("affect.habituation_factor must be in (0, 1]")
    for name, probability in config["damage"]["typo_probabilities"].items():
        if name not in ("substitution", "transposition", "deletion", "insertion"):
            raise ValueError(f"unknown typo kind: {name}")
        if not 0 <= float(probability) <= 1:
            raise ValueError(f"damage typo probability {name} must be in [0, 1]")
    for key in ("lowercase_start_probability", "drop_terminal_punctuation_probability", "self_correction_probability"):
        if not 0 <= float(config["damage"][key]) <= 1:
            raise ValueError(f"damage.{key} must be in [0, 1]")
    return config


def load_config(path):
    with open(path, "r", encoding="utf-8") as handle:
        supplied = json.load(handle)
    return validate_config(_merge(DEFAULT_CONFIG, supplied))


def env_secret(name, required=False):
    if not name:
        return None
    value = os.environ.get(name)
    if not value and required:
        raise ValueError(f"required environment variable {name} is missing")
    return value or None


def anchor_for(config, persona_id):
    overrides = config.get("personas", {}).get(persona_id, {}).get("anchor", {})
    global_anchor = config["affect"].get("anchor", {})
    return {
        name: float(overrides.get(name, global_anchor.get(name, DEFAULT_ANCHOR[name])))
        for name in DIMENSIONS
    }
