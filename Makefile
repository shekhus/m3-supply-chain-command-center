.PHONY: help synth-gold up down logs migrate metrics detect brief replay api console test lint eval
PY ?= .venv/Scripts/python
ARGS ?=

help:                 ## list targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-20s %s\n", $$1, $$2}'

synth-gold:           ## generate data/gold + ground_truth/anomalies.json (deterministic)
	$(PY) -m synth.generate_gold --out data --clean

migrate:              ## apply db/migrations to DATABASE_URL
	$(PY) scripts/migrate.py

metrics:              ## load gold and run metrics/sql in order -> metrics.daily_*
	$(PY) scripts/run_metrics.py $(ARGS)

detect:               ## detectors + attribution for one day, no LLM (DATE=YYYY-MM-DD)
	$(PY) scripts/run_detect.py --date $(DATE) $(ARGS)

brief:                ## full run to the approval interrupt (DATE=YYYY-MM-DD)
	$(PY) scripts/brief.py --date $(DATE)

replay:               ## day-by-day detection vs ground truth (FROM=... TO=... [NARRATE_SAMPLE=n])
	$(PY) scripts/replay.py --from $(FROM) --to $(TO) $(ARGS)

eval:                 ## replay scoring + narration evals -> evals/REPORT.md
	$(PY) evals/run_evals.py $(ARGS)

up:                   ## docker compose up (Postgres 5433, API 8010, console 8511)
	docker compose up -d --build

down:                 ## stop the local stack
	docker compose down

logs:                 ## follow app logs
	docker compose logs -f app

api:                  ## run the API locally
	$(PY) -m uvicorn app.main:app --reload --port 8010

console:              ## run the Streamlit console locally
	$(PY) -m streamlit run console/app.py --server.port 8511

test:                 ## pytest
	$(PY) -m pytest

lint:                 ## ruff + mypy
	$(PY) scripts/lint.py
