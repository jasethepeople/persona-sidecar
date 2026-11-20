# Agent setup

This is a single-process Python 3.10+ sidecar with no package dependencies. It binds only to loopback. Do not install Node or run a second application runtime alongside it.

1. Copy `config.example.json` to `drives.config.json`. Set `delivery.url` to the local receiver. Set `egress.mode` to `direct`, `socks5`, or `http`; for a proxy, keep credentials in `SIDECAR_PROXY_URL` (or set `egress.proxy_url`) and use `socks5h://host:port` when DNS should resolve through the proxy.
2. Provide `SIDECAR_GATEWAY_TOKEN` to the process environment. The token is required at startup. Optionally set `SIDECAR_DELIVERY_TOKEN` for a bearer token sent to the webhook.
3. The webhook receiver must deduplicate `Idempotency-Key` values. The queue retries the exact same key after a crash or ambiguous network timeout. This is the necessary receiver contract for exactly-once effects; generic HTTP cannot promise exactly-once delivery to a receiver that ignores idempotency keys.
4. Run from this directory: `python3 -m persona_sidecar --config drives.config.json`. The SQLite file is created at the configured location, with WAL journaling and full synchronous commits. Each persona's state file is written under `state.directory` with mode `0600` inside a `0700` directory.
5. Call `POST /internal/send` with `Authorization: Bearer <token>` and JSON `{ "persona_id": "default", "text": "...", "classify_text": "..." }`. Set `Idempotency-Key` for safe retries. `classify_text` is sent to the Acer Ollama model only after all response bursts are delivered. All `/internal/*` endpoints require the same gateway token and are loopback-only.

`GET /internal/context?persona=default` returns the prompt context block. `GET /internal/status` reports the durable queue. `GET /internal/audit/verify` verifies the append-only MOLT Camp 1 hash chain. The only registered delivery adapter is the reference HTTP webhook adapter.

Offline validation: run `python3 -m unittest discover -s tests -v` and `python3 -m persona_sidecar.simulate --seed 7`. These commands require only Python's standard library.
