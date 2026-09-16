# M3 Supply-Chain Command Center

**All data in this repository is synthetic, and the customer — Prairie Bend Foods — is fictional.**

> Every morning: what changed, why it matters, what to do next — code computes every number, AI narrates with
> a citation on every claim, and a person approves before anything is sent or created.

Status: **week 6 of 10 — scaffold**. The full README (customer, what was broken, how it works, evaluation, what
changes before production) is written in week 10. Until then see `docs/discovery_brief.md` and `docs/plan.md`
(section 3 is this project).

This is the second of two repositories. [`m3-trusted-data-foundation`](https://github.com/shekhus/m3-trusted-data-foundation)
produces the governed gold tables; this one consumes them. It also ships its own synthetic gold with seeded
anomalies, so it runs and demonstrates standalone.

## What it will do

```
gold.*  →  metrics/sql → metrics.daily_*  →  detectors (baseline, threshold, CUSUM) → attribution
        →  evidence pack (the only thing the model sees) → permission filter
        →  narration + validator (every claim cites a metric; every number matches the pack)
        →  drafted actions → policy gate in code → [approval] → execute → record
```

## Quickstart

Requires Python 3.12, and Docker for the full stack.

```
uv venv --python 3.12
uv pip install -r pyproject.toml --extra dev
make synth-gold     # generate data/gold + ground_truth/anomalies.json (deterministic)
make up             # Postgres (5433) + API (8010) + console (8511)
make migrate
make metrics
```

Copy `.env.example` to `.env` for local settings. With `LLM_PROVIDER=none` the brief uses its templated
fallback, which is a supported path, not a degraded one.

| Command | What it does |
|---|---|
| `make synth-gold` | standalone gold with seeded anomalies and the answer key |
| `make metrics` | run the metric SQL in order into `metrics.daily_*` |
| `make detect DATE=…` | detectors and attribution for one day, no model calls |
| `make brief DATE=…` | full run to the approval interrupt |
| `make replay FROM=… TO=…` | day-by-day detection scored against the seeded anomalies |
| `make eval` | replay scoring plus narration evals → `evals/REPORT.md` |
| `make test` / `make lint` | pytest; ruff + mypy |

## Ports

This stack deliberately avoids Project A's ports so both can run at once: Postgres **5433**, API **8010**,
console **8511**.
