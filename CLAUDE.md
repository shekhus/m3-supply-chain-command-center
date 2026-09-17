# CLAUDE.md — m3-supply-chain-command-center

Claude Code reads this file first in every session. Keep it current; it outranks memory.

## What this project is

An **operations command center** for supply-chain leaders at a fictional multi-plant protein processor ("Prairie Bend Foods") on Infor M3. Every morning it answers three questions — *what changed, why does it matter, what should we do next* — as a brief in which code computes every number, AI writes the explanation with a citation on every claim, and a person approves every action before anything is sent or created.

Portfolio project. All data is **synthetic**. Standalone mode generates its own gold tables with seeded anomalies (`make synth-gold`); integrated mode reads gold from `m3-trusted-data-foundation`. Never introduce real company names, real metrics, or client details.

Governing design document: `docs/plan.md` (Project B section). Discovery brief: `docs/discovery_brief.md`. Policy: `policy.yaml`.

## Non-negotiable principles (from the FDE playbook)

1. **The model narrates the truth; it never calculates it.** No LLM call exists in `metrics/` or `detect/`. The LLM only ever sees `evidence_pack.json`.
2. **Every claim cites a metric.** The validator in `narrate/validate.py` rejects any brief where a claim lacks a valid `metric_ref` or where a number in the text does not match the pack. Citation coverage is a hard 100% gate.
3. **Stale data is disclosed, never narrated over.** If any source is stale, the brief says so, first.
4. **Policy lives in `policy.yaml` and is enforced in code.** Allowed action types per anomaly type, thresholds, limits. The LLM cannot expand them.
5. **Human approval before any action executes.** LangGraph `interrupt_before=["execute"]`. State is in Postgres (checkpointer) and survives restarts.
6. **Permission filtering happens before the model call.** A plant manager's evidence pack contains only their plant. Not hidden in the UI — absent from the pack.
7. **The brief always ships.** LLM failure → one regeneration with the validator's error → templated (no-LLM) fallback. A missing brief is a silent failure.
8. **Nothing posts to the ERP.** Actions are tickets, investigations, and updates. Ever.

## Architecture (summary)

```
gold.* ──▶ metrics/sql/*.sql ──▶ metrics.daily_* ──▶ detect/{baseline,threshold,cusum}.py ──▶ attribute.py
   ──▶ pack/build.py (evidence_pack.json) ──▶ pack/permissions.py
   ──▶ narrate/{prompt,contract,validate,fallback}.py
   ──▶ act/{draft,policy_gate,jira_adapter}.py
   ──▶ graph.py (LangGraph: build_pack → narrate → validate → draft_actions → policy_gate → [interrupt] → execute → record)
   ──▶ deliver/{email,slack}.py · console/ (Streamlit approve/edit/reject) · ops.{runs,llm_calls,tool_calls}
   ──▶ replay/ (day-by-day evaluation vs ground_truth/anomalies.json)
```

## Repository layout

```
app/            FastAPI: routers/run.py (cron endpoint, auth), routers/approvals.py, routers/health.py
metrics/        sql/ (numbered, plain SQL), runner.py
detect/         baseline.py, threshold.py, cusum.py, severity.py, attribute.py
pack/           build.py, permissions.py, schema.py (Pydantic EvidencePack)
narrate/        prompt.py, contract.py (Pydantic Brief), validate.py, fallback.py (Jinja templates)
act/            draft.py, policy_gate.py, jira_adapter.py (mock | live), models.py
graph.py        LangGraph definition + Postgres checkpointer wiring
deliver/        email.py, slack.py
console/        Streamlit: brief view, approve/edit/reject, ops page
replay/         run_replay.py, scoring.py
synth/          gold_with_anomalies.py, anomalies.py → data/gold/, ground_truth/anomalies.json
llm/            client.py (single provider behind an interface), prompts/
evals/          run_evals.py, judge.py (LLM-as-judge on narrated samples), results/, REPORT.md (generated)
tests/          pytest; tests/failure_cases/ mirrors docs/plan.md §3.6
scripts/        every multi-step operation (Windows CMD can't do multi-line)
docs/           discovery_brief.md, plan.md, decisions.md, RUNBOOK.md
policy.yaml  docker-compose.yml  Dockerfile  Makefile  .env.example  README.md
```

## Commands

```
make synth-gold                 # standalone gold + seeded anomalies + ground truth
make up                         # docker compose (Postgres + app + console)
make migrate
make metrics                    # run metrics/sql in order → metrics.daily_*
make detect DATE=2026-06-15     # detectors + attribution for one day (no LLM)
make brief DATE=2026-06-15      # full run through graph up to the approval interrupt
make replay FROM=2025-03-01 TO=2026-08-31 [NARRATE_SAMPLE=20]
make test
make eval                       # replay scoring + citation/numeric/judge evals → evals/REPORT.md
make lint
```

Windows: use Git Bash or the `python scripts/<name>.py` equivalents in `scripts/README.md`.

## Coding conventions

- Python 3.12, type hints, Pydantic v2 for EvidencePack, Brief, Action, and every LLM contract.
- Metric SQL: one file per metric, numbered for dependency order, idempotent (create-or-replace / merge by date).
- Detectors return `Anomaly(metric, segment, window, magnitude, detector, severity, evidence_refs)`. Severity is computed in `severity.py`, never by the LLM.
- Attribution returns `Driver(segment, contribution_pct, direction)` computed by volume-weighted decomposition. The number in the brief must be this number.
- LLM calls only via `llm/client.py`; every call logged to `ops.llm_calls` with validator outcome.
- Narration prompt returns strict JSON; parse with `narrate/contract.py`; validate with `narrate/validate.py`; at most one regeneration.
- Jira adapter: `JIRA_MODE=mock` default. Live mode requires env; timeouts produce `FAILED_RETRYABLE`, never an exception to the user.
- Every run is idempotent by `run_date`.
- Tests: unit per detector with synthetic series; a hand-computed OTIF week test; validator tests with deliberately bad briefs; policy-gate adversarial tests; graph resume-after-restart test.
- Commit messages state the decision.

## Definition of done (per task)

- Tests green, `make lint` clean.
- Behaviour change → line in `docs/decisions.md`.
- New detector / validator rule / action type → eval case added.
- Any new LLM-facing field → contract updated + validator test.

## Session ritual

1. Read this file, `docs/plan.md` (current week), `docs/decisions.md` (last 10), `policy.yaml`.
2. State the week's definition of done in one line. Propose 1–3 tasks. **Wait for approval before coding.**
3. One task per turn; tests alongside.
4. `make test`; `make replay` or `make eval` when relevant; paste output; analyse before fixing.
5. Append to `docs/decisions.md`. Commit.

## Things not to do

- Do not compute, round, aggregate, or "estimate" any number in a prompt. If the number isn't in the pack, it isn't in the brief.
- Do not widen `policy.yaml` from code or prompts.
- Do not bypass the interrupt for "just this test" — write a test that resumes the graph instead.
- Do not narrate during replay unless `NARRATE_SAMPLE` is set (cost control).
- Do not hard-code holidays or thresholds outside `policy.yaml`.
- Do not add real company names, client metrics, or engagement details.
- Do not pin unverified package versions; check LangGraph checkpointer and SDK docs first.
- Do not modify `ground_truth/`.

## Current status

`STATUS: week 8 in progress — weeks 1–7 done (scaffold, generator, metric SQL, detectors, attribution, replay: recall 100% / precision 80% / attribution 100% / decoy never HIGH). B-7 evidence pack (every quotable number a named fact, rounding done once in code) + B-8 permission filter (the audience is a parameter of build_pack, so another plant is absent from the prompt, not hidden). B-9 narration: contract where a claim cannot exist without a citation, validator checking every number against the pack, one correction carrying its complaints, then a templated brief that always ships. Live sample of 20 briefs on Groq openai/gpt-oss-120b: **citation coverage 100%, first-pass validity 100%, fallback 0%, $0.0017 per brief** (first run was 35% / 25% — the fixes were to the checker and the plumbing, not the standard; see docs/decisions.md B-008). 141 tests. Next: week 9 — action drafting, policy gate, LangGraph approval with a Postgres checkpointer and interrupt, Jira adapter (mock + live), console, delivery. Week-9 DoD: cron → brief → console → approve → mock Jira ticket, and a restart mid-approval that resumes.`
