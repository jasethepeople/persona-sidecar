# Stage 35 — affect

**Position:** runtime → **35-affect** → persistence. One Python service owns all personas; there is no per-persona process and no Node runtime. State is six dimensions in one private JSON file per persona: irritability, fatigue, elation, longing, dejection, and play. The filename is derived from a SHA-256 of the persona ID; writes use a temporary file, fsync, and atomic rename. SQLite remains the durable store for queue, audit, rate-limit, and classifier-job records.

The module keeps fast `base` values and slower `mood` baselines, clamped to `[0, 1]`. Fast values ease toward mood using dimension-specific exponential time constants. Mood consolidates toward the fast value at an arousal-gated rate and then returns toward the configured persona anchor on a slower timescale. Irritability, fatigue, and dejection have slower mood return (negativity bias). Repeated classifier labels within 15 minutes are multiplied by `0.7 ** prior_occurrences`.

Only labels from the local allowlist can change state. Classifier output is parsed and validated before any state transaction. The Ollama worker runs asynchronously after the final burst has been delivered; a failed or malformed classifier result records a failed job and leaves the persona's state file untouched. The send scheduler does not wait for Ollama.

`GET /internal/context?persona=<id>` returns the pre-formatted `[affect]` block for prompt assembly. It describes the internal state, not user intent; see `drives-personas-context.md`.
