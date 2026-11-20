# Stage 60 — acceptance checks

The following checks are added by PATCH 02. Tests 1–15 belong to the surrounding project plan and are not redefined here.

| ID | Check | Pass condition |
|---|---|---|
| 16 | Affect bounds and decay | All six dimensions clamp to `[0, 1]`; after elapsed time, fast state and mood return toward their persona anchors. |
| 17 | Habituation | A repeated classifier label inside 15 minutes applies a smaller delta (`0.7` per prior occurrence). |
| 18 | Classifier isolation | An Ollama/classifier failure leaves the persona state unchanged; an already-scheduled send succeeds without waiting for classification. |
| 19 | Persistence and isolation | State remains after reopening the sidecar and changing one persona does not change another persona's state file. |

The seeded offline simulation is `python3 -m persona_sidecar.simulate --seed 7`. The full standard-library suite is `python3 -m unittest discover -s tests -v`. Both run without Ollama, network access, or an installed adapter receiver.
