# Scripts

Every multi-step operation is a script, because Windows CMD cannot run multi-line shell. `make <target>` and the
script are the same thing; use whichever suits your shell.

| Make target | Equivalent |
|---|---|
| `make synth-gold` | `.venv\Scripts\python -m synth.generate_gold --out data --clean` |
| `make migrate` | `.venv\Scripts\python scripts/migrate.py` |
| `make metrics` | `.venv\Scripts\python scripts/run_metrics.py` |
| `make detect DATE=2026-06-15` | `.venv\Scripts\python scripts/detect.py --date 2026-06-15` |
| `make brief DATE=2026-06-15` | `.venv\Scripts\python scripts/brief.py --date 2026-06-15` |
| `make replay FROM=2025-03-01 TO=2026-08-31` | `.venv\Scripts\python scripts/replay.py --from 2025-03-01 --to 2026-08-31` |
| `make eval` | `.venv\Scripts\python evals/run_evals.py` |
| `make test` | `.venv\Scripts\python -m pytest` |
| `make lint` | `.venv\Scripts\python scripts/lint.py` |

Scripts that talk to a model or a ticketing system say so in their `--help`, and default to the mock or
no-model path.
