# Persona sidecar

A loopback-only, config-driven Python sidecar for agent message delivery and compact per-persona affect state. It is one process and uses only Python's standard library. The durable SQLite database stores the delivery queue, classifier jobs, rate limits, and audit chain; each persona's affect state is an atomic, private JSON file.

## Included

- `DeliveryAdapter` interface with one reference HTTP webhook adapter. The daemon scheduler persists and retries ordered message bursts.
- Crash recovery retries every burst with a stable `Idempotency-Key`. Exactly-once effects require the webhook receiver to deduplicate that key; no HTTP client can guarantee exactly-once effects against a non-idempotent receiver.
- Cumulative log-normal per-burst delay, scaled by configured reading speed; seeded timing and text damage for reproducible tests.
- Kind-based substitutions, transpositions, deletions, and insertions, plus optional lowercase starts, dropped terminal punctuation, and separate self-correction bursts.
- Gateway bearer-token validation, loopback enforcement, per-token rate limits, SQLite WAL/full-sync queue, and a SHA-256 append-only MOLT Camp 1 audit chain.
- Egress interface for direct, HTTP proxy, and SOCKS5/SOCKS5h requests. Before each send attempt it observes the public egress IP and writes the observation (or observation failure) to SQLite and the audit chain.
- Stage 35 affect model: six dimensions only, one atomic JSON state file per persona, two-timescale decay, habituation, negativity bias, arousal-gated mood consolidation, and a prompt-ready context endpoint.
- Async post-delivery classifier defaults to a local deterministic `rules` backend; the optional `ollama` backend keeps a configurable model and an 8-second timeout. Classification failures never block sending or mutate affect state.
- Offline seeded simulation and stage 60 checks 16–19.

## Run and verify

The example config selects `classifier.backend: "rules"` to keep message handling independent of model-server latency. To compare local Ollama candidates, set `backend` to `ollama`, keep the Acer-local endpoint `http://127.0.0.1:11434/api/chat`, and try `qwen2.5:0.5b` or `qwen2.5:1.5b` as the `model`. `qwen2.5:3b` remains the previously measured slow baseline. Run `python3 scripts/classifier_bench.py --backend both --model qwen2.5:0.5b` (repeat for `1.5b`) to compare the same labelled fixtures. Do not switch to an Ollama model unless the endpoint is reachable and the local measurement beats the 8-second job budget.

See `AGENT_SETUP.md` for setup and the webhook's idempotency contract. Copy `config.example.json` to `drives.config.json`, provide `SIDECAR_GATEWAY_TOKEN`, then run:

```sh
python3 -m persona_sidecar --config drives.config.json
```

Run tests and simulation offline:

```sh
python3 -m unittest discover -s tests -v
python3 -m persona_sidecar.simulate --seed 7
```

## Internal API

- `POST /internal/send` — JSON fields `persona_id`, `text`, optional `classify_text`; supports `Idempotency-Key` header; returns 202 after durable enqueue.
- `GET /internal/context?persona=<id>` — preformatted `[affect]` prompt block.
- `GET /internal/status` — queue counts and recent delivery metadata.
- `GET /internal/audit/verify` — audit-chain integrity result.
- `GET /internal/health` — authenticated local health check.

Every endpoint is loopback-only and token-protected. Keep the gateway token in a secret manager or process environment, never in the config file.

## Project documents

- `stage-35-affect.md` — affect model and its place in the runtime pipeline.
- `stage-60-tests.md` — acceptance checks 16–19.
- `drives-personas-context.md` — how agents should interpret injected context.

## Attribution and license

The affect model's mathematical structure is adapted from [Drivesoid](https://github.com/A1batr055/Drivesoid), by A1batr055. Adapted material is licensed CC BY-NC-SA 4.0; see `LICENSE`. The adaptation retains two timescales, habituation, negativity bias, and arousal gating, while using the requested six dimensions rather than Drivesoid's larger model.
