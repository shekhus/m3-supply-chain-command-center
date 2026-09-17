# How this was built — a developer's tour

*The companion to [EXPLAINER.md](EXPLAINER.md). That document answers "why does it work this way". This one
answers "what is in here, in what order was it built, and where do I put my hands".*

*Written for somebody who has just cloned this and would like the sixteen folders to stop being a wall. No
prior knowledge assumed.*

---

## Chapter 0 · The spine

This repository is easier than most, because the data only flows one way and every folder is one station on
that line. Read this sentence and you have the architecture:

> **Numbers → findings → a pack → words → proposed actions → a person → a ticket.**

Seven stations. One folder each:

| Station | Folder | The question it answers |
|---|---|---|
| Numbers | `metrics/` | What happened yesterday, as a number? |
| Findings | `detect/` | Which of those numbers is worth a person's attention? |
| A pack | `pack/` | What exactly is the model allowed to see and say? |
| Words | `narrate/` | How do we turn findings into prose that can be checked? |
| Proposals | `act/` | What may we *do* about it, and who says so? |
| A person | `graph.py` + `console/` | Where does a human approve, and how does that survive a restart? |
| A ticket | `act/jira_adapter.py` + `deliver/` | How does it leave the building? |

Everything else supports those: `synth/` makes the world, `replay/` and `evals/` measure it, `app/` exposes it
over HTTP, `db/` holds the schema, `tests/` proves it.

If you only remember one thing from this document, remember the spine. Every file you open belongs to one
station, and you can ignore the other six while you're there.

---

## Chapter 1 · The map

| Folder | Lines | What it is |
|---|---:|---|
| `synth/` | 1,347 | **The fake company** — the same world as the first project, same seed, plus six planted anomalies and an answer key. |
| `metrics/` | 171 + SQL | **Seven daily series**, computed by numbered plain SQL. The Python here just runs the SQL in order. |
| `detect/` | 1,261 | **Three detectors, severity, attribution.** The biggest module, and the one with the real thinking in it. |
| `pack/` | 539 | **The evidence pack** and the permission filter — everything the model will ever see. |
| `narrate/` | 621 | **The contract, the prompt, the validator, the templated fallback.** |
| `act/` | 737 | **Drafting, the policy gate, the ticket adapter, the durable record.** |
| `graph.py` | ~260 | **The approval workflow** — the pause that survives a restart. A single file at the root, deliberately. |
| `service.py` | ~150 | **The wiring.** The only module that knows Postgres, the model provider and the tracker all exist. |
| `replay/` | 498 | **Scoring against the answer key** — events, matching rules, the scorecard. |
| `evals/` | 428 | **The measurements and the generated report.** |
| `app/` | 466 | **The HTTP API** — three endpoints for briefs, two for ops. |
| `console/` | 217 | **The Streamlit console** where a person approves. Talks to the API, never the database. |
| `deliver/` | 224 | **Email and Slack** — rendering and sending, where failure is a status and never an exception. |
| `llm/` | 258 | **The only place this repo calls a model**, copied from the first project. |
| `db/` | 131 + SQL | **Migrations**, numbered and checksummed. |
| `scripts/` | 339 | **Entry points**: the morning run, detection, replay, migration. |
| `tests/` | 3,707 | **261 tests.** Again the biggest folder, again on purpose. |

**Two files live at the repository root rather than in a folder**, which is unusual enough to explain.
`graph.py` is the approval workflow and `service.py` is the wiring that assembles everything it needs. Both are
about *composition* — they belong to no single station, and burying them inside one would suggest otherwise.

---

## Chapter 2 · Follow one morning all the way through

Trace a single day's brief. Every file it touches, in order.

**06:00, the scheduler fires.**
→ `scripts/morning.py` — the entry point, which runs in-process rather than calling its own API.

**Assemble the collaborators.**
→ `service.py` builds everything the workflow needs: a pack builder, a narrator, an executor, the open-action
query, the recorder. This is the only place the real implementations are chosen, so a test can supply its own.

**Start the workflow.**
→ `graph.py` — `build_pack → narrate → draft_actions → policy_gate → ⟪PAUSE⟫ → execute → record`.

**Compute what happened.** (Already done, nightly — the brief reads it.)
→ `metrics/sql/*.sql` produced the daily series; `metrics/runner.py` ran them in order.

**Find what's worth saying.**
→ `detect/series.py` shapes each segment's history into arrays.
→ `detect/baseline.py`, `detect/threshold.py`, `detect/cusum.py` each give a verdict.
→ `detect/severity.py` combines them and applies the four gates.
→ `detect/attribute.py` works out which lane is driving it.

**Build what the model may see.**
→ `pack/build.py` assembles items, drivers, freshness and the flat fact map; `pack/permissions.py` decides
whose brief this is, *before* any item exists.

**Write it.**
→ `narrate/run.py` orchestrates; `narrate/contract.py` defines the only shape the model may return;
`llm/client.py` makes the call; `narrate/validate.py` checks every number against the pack; on failure, one
correction, then `narrate/fallback.py` builds a templated brief instead.

**Propose actions.**
→ `act/draft.py` writes the ticket bodies from the pack's facts, in code.
→ `act/policy_gate.py` judges every one of them against `policy.yaml`.

**Stop.** The workflow pauses. Nothing has left the building.
→ `act/store.py` records the brief and its proposals now, not later — so tomorrow's duplicate suppression can
see them.
→ `deliver/render.py` and `deliver/send.py` send the notification, linking to the console.

**14:00, a person approves.**
→ `console/app.py` → `app/routers/brief.py` → `service.apply_decisions` → `graph.decide`.

**Now it acts.**
→ `act/jira_adapter.py` creates the ticket. A timeout becomes a retryable status, not an exception.
→ `act/store.py` writes what happened.

**Next morning, somebody checks it worked.**
→ `app/routers/ops.py` over the views in `db/migrations/0007_ops_views.sql`.

Eleven files doing the real work. Read them in that order and you have the system.

---

## Chapter 3 · The development loop

The same rhythm as the first project, because it worked:

**Read** `CLAUDE.md`, the current week in `docs/plan.md`, the last few entries in `docs/decisions.md`.
**Propose** one to three tasks and agree them before writing code.
**Build one task**, tests alongside — not after.
**Run** `make lint` and `make test`, paste the output, *read it before fixing*.
**Record** a decision with its alternatives and evidence.
**Commit** with a message stating the decision rather than the diff.

Fifteen decisions, 261 tests, five weeks.

One difference from the first project, and it changed the shape of this one: **several tasks ended by measuring
something, and the measurement disagreed with the design.** The 2% precision replay, the attribution ranking
that named the wrong lane, the validator that was wrong three times out of four. Each of those turned into its
own decision entry with the before and after numbers.

That's not a sign the plan was bad. It's the plan working — the measurement steps existed *so that* they could
disagree.

---

## Chapter 4 · The build order, and why that order

Five weeks, weeks six to ten of the overall build. Each week's output is the next week's input.

### Week 6 — Scaffold, world, metrics

**Built:** the repo, copied infrastructure, the generator, and the seven metric tables.

**Why first:** everything downstream reads `metrics.daily_*`. Until those exist and are trustworthy, there is
nothing to detect.

**Key files:** `db/migrate.py`, `synth/generate_gold.py`, `metrics/sql/*.sql`, `metrics/runner.py`

**The decision worth copying:** the infrastructure was **copied** from the first project, not imported as a
library (`B-001`). A shared package would have made this repo unable to start without the other one — exactly
what standalone mode exists to prevent.

**The test that matters:** the metric SQL recomputes on-time and in-full from raw fields rather than trusting
the upstream flags, and a test asserts the two agree. Zero disagreements across 51,571 rows (`B-003`).

### Week 7 — Detection, attribution, replay

**Built:** three detectors, severity, attribution, and the replay harness that scores them.

**Why here:** nothing can be narrated until there is something to narrate, and nothing can be trusted until it
has been scored against the answer key.

**Key files:** `detect/baseline.py`, `detect/threshold.py`, `detect/cusum.py`, `detect/severity.py`,
`detect/attribute.py`, `replay/scoring.py`, `replay/run_replay.py`

**What happened:** the first replay scored **2% precision**, and four things were wrong — three of them my
thresholds and one a genuine CUSUM bug (`B-006`). This week's real output wasn't the detectors, it was the
rewritten `policy.yaml`.

**A performance note you'll meet if you touch `detect/`:** a replay walks 535 days across ~173 segments. The
first version rebuilt each segment's baseline inside every detector for every day, which made it quadratic.
`detect/series.py` now holds each segment as sorted numpy arrays and the baseline is computed once per day, for
everyone. 25 seconds instead of minutes.

### Week 8 — The pack, permissions, narration

**Built:** the evidence pack, the permission filter, the contract, the prompt, the validator, the fallback.

**Why here:** the pack can only be built once you know what a finding looks like.

**Key files:** `pack/schema.py`, `pack/build.py`, `pack/permissions.py`, `narrate/contract.py`,
`narrate/validate.py`, `narrate/fallback.py`, `narrate/run.py`, `llm/prompts/narrate.md`

**What happened:** the first live sample of twenty briefs had a 25% fallback rate. Three of the four causes were
in my validator, not the model (`B-008`). Fixing the checker took first-pass validity from 35% to 100%.

### Week 9 — Actions, approval, the outside world

**Built:** drafting, the policy gate, the workflow with its interrupt, the ticket adapter, the API, the console,
delivery.

**Why here:** you can only act on something you have already computed, narrated and validated.

**Key files:** `act/draft.py`, `act/policy_gate.py`, `graph.py`, `act/jira_adapter.py`, `act/store.py`,
`service.py`, `app/routers/brief.py`, `console/app.py`, `deliver/send.py`

**The test that justifies the framework:** start a run, pause, **throw away the compiled graph, the checkpointer
and the connection**, then resume from Postgres with a fresh set and execute (`B-010`).

### Week 10 — Observability, failure cases, report, deploy

**Built:** the ops views and alerts, the eight failure-case tests, the generated evaluation report, the
deployment.

**Why last:** you can only observe a system that does something, and only report numbers that exist.

**Key files:** `db/migrations/0007_ops_views.sql`, `app/routers/ops.py`, `tests/failure_cases/`,
`evals/run_evals.py`, `docs/RUNBOOK.md`

**Found by deploying:** the ops page showed cost `$0.0000` beside usage of `$0.0029`, because the pack stored a
*role* where the run record stored a *user id*. Every local test passed, because every local test used the same
wrong value on both sides (`B-014`).

---

## Chapter 5 · The plumbing, explained once

Mostly identical to the first project — same `pyproject.toml` + `uv`, same thin Makefile calling `scripts/`,
same numbered migrations with checksums and an advisory lock, same `.env`-overrides-shell rule, same
`postgres`-marked tests, same `scripts/lint.py`. Chapter 5 of
[the other BUILD.md](https://github.com/shekhus/m3-trusted-data-foundation/blob/main/docs/BUILD.md) covers all
of that.

Four things are specific to this repository.

### `policy.yaml` — the file the business owns

Thresholds, seasonality, the detection floors, which actions are allowed for which problems, and the limits on
a brief. Read by `detect/config.py` and `act/policy_gate.py`, never hard-coded.

Every number in it carries a comment saying where it came from — most of them a measured percentile of the
series they govern, after the 2% precision run taught me that guessed thresholds fire on half of all days.

**Rule of thumb:** if you're about to write a number in a `.py` file that a business person might one day want
changed, it belongs here instead.

### `METRICS_SOURCE` — Postgres or parquet

A deployment reads the tables `make metrics` filled. The standalone demo (and the tests) read the parquet files
the generator wrote. One environment variable, honoured in `service.load_world`.

This is why the test suite can run against a fresh database that has the schema but no metrics in it.

### `graph_checkpoints` — a schema the framework owns

The workflow's paused state lives in its own database schema, managed entirely by the library. The business
record — briefs, actions, decisions — lives in `ops.*` and is written by `act/store.py`.

**They are deliberately separate.** One is a paused machine whose shape a library upgrade may change. The other
is the answer to "what did we send on the 15th and who approved it", which has to outlive any such upgrade.

### `APP_ROLE` — one image, three jobs

`scripts/start.sh` branches on it: unset runs the API, `console` runs Streamlit, `cron` runs one morning and
exits. One image means the three can never drift apart, and a cron service that *exits* is what a scheduler
expects — one that stays running is one that ran once.

---

## Chapter 6 · Recipes

**Add a metric.** New numbered file in `metrics/sql/`, merging by its own key so a rerun is idempotent. Add the
grain to `detect/series.py` if you want it watched, and a threshold to `policy.yaml` if crossing a line should
raise an incident.

**Change a threshold.** Edit `policy.yaml`, then **run `make replay` and look at what it did to precision and
recall before you keep it.** That takes two minutes and it is the whole reason the replay harness exists.

**Add a detector.** A module in `detect/` returning a verdict, wired into `detect/severity.py`. Write the paper
test first — a series whose right answer you can work out by hand.

**Add an action type.** Extend the type in `act/models.py`, add it to `policy.yaml`, teach `act/jira_adapter.py`
where it goes. The gate will refuse it everywhere you forgot.

**Change what the brief says.** If it's wording, it's `llm/prompts/narrate.md`. If it's a *number* the brief
needs, it must first exist as a fact in `pack/build.py` — otherwise the validator will reject the sentence,
correctly.

**Add an ops alert.** A view in a new migration, a check in `app/routers/ops.py`, and **a section in
`docs/RUNBOOK.md` with the same name**. An alert without a page is a pager without an answer.

---

## Chapter 7 · If you have thirty minutes

1. **`README.md`** — five minutes.
2. **`docs/EXPLAINER.md`** Chapters 0–3 — ten minutes. The lane collapse the dashboard can't see.
3. **`policy.yaml`** — five minutes. The whole business configuration in one readable file, with its reasons in
   comments.
4. **`detect/severity.py`** — five minutes. Where three opinions become one verdict, and where four gates decide
   what's worth a person's morning.
5. **`tests/failure_cases/test_failure_cases.py`** — five minutes. Eight named failures, each with the expected
   behaviour in its docstring.

---

## Chapter 8 · What I would tell someone taking this over

**`policy.yaml` is the product.** Most requests to "change how it behaves" are changes to that file, and the
code is arranged so that stays true. If you find yourself adding a business number to a `.py` file, stop.

**Run the replay after touching `detect/` or `policy.yaml`.** Two minutes, and it's the difference between a
change you measured and a change you hope about.

**The validator is stricter than your intuition, and it has been wrong.** Three of four fallbacks in the first
sample were my checker mishandling typography, not the model inventing numbers. When something is rejected,
check the checker first.

**The failure-case suite is the specification.** `tests/failure_cases/` has one test per row of the plan's
table, named after the failure. If you're changing behaviour, that's where to look for what must stay true.

**Nothing writes to the ERP, ever.** A test greps the entire codebase for anything resembling it. If you need a
write-back, that's an architecture conversation, not a feature.

**The boring parts are the point.** Plain SQL, numbered migrations, one client for the model, tests named as
sentences. None of it is clever, which is why a change is fifteen minutes rather than an afternoon of
archaeology.

---

*The first half of this system — how the numbers became trustworthy — is in
[the foundation's BUILD.md](https://github.com/shekhus/m3-trusted-data-foundation/blob/main/docs/BUILD.md).*
