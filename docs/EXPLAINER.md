# Explainer — M3 Supply-Chain Command Center

Every decision in this project, in four layers:

> **What it is** — in plain words, assuming nothing.
> **Why it exists** — the failure it prevents. This is the layer that matters.
> **What was chosen, and what was rejected** — the alternatives, and why they lost.
> **The number** — the evidence, and where to check it.

Each section ends with **"the sentence"** — what to say out loud when somebody asks.

All data is synthetic; Prairie Bend Foods is fictional. References like `B-006` point at
[`decisions.md`](decisions.md). The companion document is
[Project A's explainer](https://github.com/shekhus/m3-trusted-data-foundation/blob/main/docs/EXPLAINER.md):
A governs the data, B acts on it.

---

## The problem, before any code

Leaders open eight to ten dashboards each morning. When something looks red, somebody investigates by hand,
decides whether it matters, and raises a ticket. Signal to action: often days.

The specific failure this project is built around is sharper than "too many dashboards":

**A lane-level collapse is invisible in the numbers everybody watches.** One customer's OTIF at one plant falls
from 93% to 48% for three weeks — and the company total never moves outside its own noise.

There is a test asserting exactly that: `test_the_company_total_never_sees_the_lane_collapse_at_all`. The
company total does not even register a *finding* on those days, because the movement is below the level policy
calls material at that grain.

> **The sentence:** "Three weeks of a customer being let down, and the number on the executive dashboard never
> moved. That's not a reporting problem you fix with another chart."

---

## Week 6 — The foundations

### 6.1 Copied from Project A, not imported

**What it is.** The migration runner, settings loader, role auth, lint runner and CI workflow are copies of A's,
not a shared library (`B-001`).

**Why it exists.** A shared package would make B unable to start without A — which is precisely what
standalone mode exists to prevent. Two repositories that each run alone are worth more than 300 lines saved.

> **The sentence:** "Shared code would have coupled the demo to the other repo. Copying was the cheaper
> dependency."

### 6.2 The same world, generated twice

**What it is.** B generates its own gold from the same generator and seed as A, so both repositories describe
one company (`B-002`). Six anomalies are seeded, including a **decoy**.

**Why it exists.** If B invented its own world, the two projects would stop telling one story and A's answer
keys would no longer describe B's data.

**The gate that matters:** generation *fails* if a seeded anomaly is not measurably present. Detection recall,
precision, lead time and attribution are all scored against that answer key — so a ground truth that quietly
lied would invalidate every later measurement, silently.

**The number.** B's `fact_delivery` fingerprint equals A's **row for row**. All six anomalies verified present,
including the decoy's genuine −0.19 dip.

> **The sentence:** "The generator refuses to write an answer key it can't verify. Otherwise every number
> downstream is measured against a lie."

### 6.3 The metric layer recomputes what it was given

**What it is.** Seven daily metric tables, computed by numbered plain SQL, merged by key so a rerun is
idempotent (`B-003`).

**Why it exists — the interesting part.** The SQL **recomputes on-time and in-full from the raw fields** rather
than trusting the flags gold already publishes, and a test asserts the two agree. That proves this project
implements the same definition rather than assuming it — the failure where two systems quote the same KPI and
quietly mean different things.

**Chosen / rejected.** Plain numbered SQL. Rejected: dbt (a second toolchain and a second definition language
for eight models); computing metrics in pandas at request time (nothing to test independently, nothing to
index).

**The number.** **0 disagreements across 51,571 rows.** Constants (catch-weight tolerance, at-risk window) come
from `policy.yaml` via a `metrics.constants` table, because a `CREATE VIEW` cannot take a bind parameter and a
tolerance hard-coded in three files will one day disagree with itself.

> **The sentence:** "I didn't trust the upstream flags. I recomputed them and tested that we agree — that's how
> you find out two systems mean different things by the same word."

---

## Week 7 — Detection, attribution, and the measurement that rewrote the policy

### 7.1 Three detectors, because each misses a different shape

**What it is.** A day-of-week-adjusted rolling baseline (z-score), policy thresholds with consecutive-day
rules, and CUSUM change-point detection — severity is the strongest verdict any of them gives (`B-004`).

**Why it exists.** The baseline sees a *day*. CUSUM sees a *shift*: a drop of half a standard deviation a day
that has not gone away in a week — invisible to a z-score, and exactly the shape of a weight shortfall or
stalled inventory. The threshold rule sees neither; it sees the line the business drew.

**The two corrections that separate a detector from an alarm:**
- **Day of week.** Comparing a Monday against a mean containing four Sundays produces a signal about the
  calendar, not the business.
- **Holidays.** Excluded from the baseline, so a shutdown does not quietly lower the bar for the fortnight
  after it.

**The gates.** Severity is capped by thin volume (a rate from three lines is arithmetic, not information),
immaterial movement, no corroboration, and the holiday allowance.

> **The sentence:** "Three detectors because a cliff, a level and a drift are three different shapes, and a
> detector that only sees one of them is going to miss the interesting week."

### 7.2 Attribution is an identity that closes

**What it is.** A rate at a coarse grain is a weighted average of the same rate one level down, so the movement
splits exactly into a **rate effect** (this segment got worse) and a **mix effect** (this segment is a bigger
share of the volume) (`B-005`).

**Why it exists.** "This lane is 71% of the drop" is only publishable if the contributions sum to the movement.
The identity closing is what licenses the percentage.

**Chosen / rejected — measured, not assumed.** Ranking drivers by the *total* contribution made PRIMALS the top
driver of the weight-basis shortfall — a pure volume-mix artefact — and hid CASE-READY, the seeded cause.
Ranked by the **rate effect**, the top driver is the seeded segment every time. Rejected: Shapley values
(defensible, slower, and nobody in the room can check the number).

**The quotable number is the share of the performance change**, not of the whole movement: a share of the whole
can exceed 100% or flip sign when the mix moved the other way. *"PLT-02 accounts for −52% of the drop"* is
arithmetically true and unusable.

> **The sentence:** "The 71% isn't a phrase the model chose. It's a decomposition that adds up — and I rank by
> who got worse, not by who moved the average."

### 7.3 The replay that said 2%, and the four things it found

**What it is.** A harness that walks 535 days, merges flags into events, and scores recall, precision, lead time
and attribution against the answer key (`B-006`).

**Why it exists.** Everything above is a hypothesis until something measures it over history.

**What the first run said: recall 100%, precision 2%.** 383 HIGH events, 377 with nothing behind them. Four
causes, and only one was subtle:

1. **The thresholds in `policy.yaml` were guesses, and the data said so.** OTIF's "high" line of 0.90 sits at
   the measured *median* of 0.905 — it fired on half of all days. Every level is now set from the observed
   distribution, with the percentile written beside it in the file.
2. **A line drawn for the company is not the line for one lane.** A lane's daily rate is 0 or 1 (median 1.00,
   tenth percentile 0.00), so no absolute line means anything there. A lane is now judged on its **seven-day
   volume-weighted rate against its own 28-day norm** — compared like with like.
3. **An incident is a policy breach, not a statistical shift.** The statistical detectors raise WARN on their
   own; only a line in `policy.yaml` makes something HIGH. Without that rule they produced **322 of 365** HIGH
   flags — nearly all real movements nobody needed waking for.
4. **CUSUM never reset after signalling** — a genuine bug against the textbook procedure, which reported the
   same change every day for a fortnight.

**The refusal worth more than the fixes.** Backlog has **no HIGH rule**. Measured: the seeded order surge peaks
at **z = 2.81**, while ordinary swings reach **4.09, 4.39 and 5.04**. No line admits the real one without
admitting all of them. So the rule was removed and what would fix it written down, rather than tuned until the
answer key looked good.

**The honesty mechanism.** The floors were tuned on the first half of the history, so the report scores the
**held-out half separately**: 2/2 found, **100% precision**, zero false positives.

**The number.** Recall **100%**, precision **80%**, attribution top-1 **100%**, median lead time **1 day**,
decoy raised HIGH **0 times** — five HIGH events in eighteen months, four of them the seeded anomalies.

> **The sentence:** "My first replay scored 2% precision. Three of the four causes were my thresholds being
> guesses — and one rule I deleted entirely, because the measurement showed it could never work."

---

## Week 8 — The pack, the filter, and the citation gate

### 8.1 The evidence pack is the model's entire world

**What it is.** One JSON per run: the items, their drivers, the detector's reasons, freshness — and a **flat map
of every quotable number**, each with the exact string to use (`B-007`).

**Why it exists.** Three properties make the validator possible at all:
- **Every number has a stable reference** (`I1.value`, `I1.driver1.share`). Flat, not nested: a validator
  resolving a path through a tree is a validator with bugs in it.
- **Rounding happens once, in code.** The model is asked to copy `74.1%`, never to format `0.74138`. A model
  asked to render a rate will sometimes write 74%, sometimes 74.14%, and occasionally 74.8% — and the last one
  is indistinguishable from the others inside a confident paragraph.
- **Nothing else is in scope.** No tables, no history to average. A number that is not a fact cannot be said
  and pass the validator.

**A subtlety worth knowing:** two units both read as "points". `points` is already percentage points (yield
variance); `rate_change` is a difference between two rates and must be multiplied out. Getting that wrong turns
0.5 points into 50.0 points — which the first version did.

**A holiday is named before the numbers are.** Otherwise the 4 July brief opens with OTIF at 34.6% and no
explanation, and the reader either panics or learns to ignore the brief every July.

> **The sentence:** "The model can't average anything, because it never receives anything to average."

### 8.2 The permission filter is a parameter, not a step

**What it is.** `build_pack(..., audience)` narrows *before* items exist, so another plant's numbers are absent
from the pack — and therefore from the prompt, the model's context and the logs (`B-007`).

**Why it exists.** Filtering at display leaves the data in the prompt, one bug away from the reader. A test
searches a PLT-01 manager's serialised prompt for `PLT-02` and `C000031` and requires zero hits.

**An unknown user is refused, not defaulted.** Defaulting to "a plant manager for no plants" produces an empty
brief — which looks like a quiet morning. That is the failure that hides itself.

> **The sentence:** "It's not hidden in the UI. It was never in the prompt."

### 8.3 A claim cannot exist without a citation

**What it is.** `metric_ref` is a required field on every claim, so an uncited claim is a shape the model cannot
return. The validator then chases the harder failure: **a claim that cites a real fact and states a different
number** (`B-008`).

**Why it exists.** A wrong number under a correct citation is wrong, cited and completely convincing — the worst
of the three.

**Then one correction, then the template.** Past two attempts the honest options are a flatter brief that is
certainly right, or an argument with a language model at six in the morning. The templated brief is validated
like everything else, and the fallback rate is published — because a fallback rate nobody publishes quietly
becomes 100%.

**What the first live sample taught.** 20 briefs: citation coverage 100%, but first-pass validity **35%** and a
**25% fallback rate**. Only one of the four causes was the model's:
- The model writes `PLT‑02` with a **non-breaking hyphen**; my validator compared bytes, saw a bare "02", and
  rejected good sentences. Three of four fallbacks were this.
- "OTIF fell by 9.4 points" was rejected because the fact is −9.4 — a validator enforcing notation, not truth.
- A union type in the strict JSON schema was rejected by the provider outright.
- `max_tokens` was too low, so a six-item brief came back truncated.

The same sample also found a **pack** bug: volume labels keyed by metric, so a plant × group fill rate weighted
by *lines* was labelled "ordered pounds" — a wrong label on a right number, which survives every numeric check.

**The number, after those fixes on the same twenty days:** citation coverage **100%**, first-pass validity
**100%**, fallback **0%**, **$0.0019** per brief. The standard did not move: the adversarial tests still reject
a wrong number under a right citation, an invented ref, a talked-up severity and an undisclosed stale source.

> **The sentence:** "My fallback rate was 25%. Three of the four causes were my checker being wrong about
> typography, not the model being wrong about facts."

---

## Week 9 — Proposing, approving, acting

### 9.1 The gate does not care who wrote the action

**What it is.** Actions are drafted in code from the pack's facts; the policy gate then judges every action
regardless of origin — drafter, future model, console retry, test (`B-009`).

**Why it exists.** A drafter that can only produce allowed actions is one refactor away from producing a
disallowed one. The gate is what will still be true afterwards.

**Blocked actions are kept, with their reason**, so a policy that has drifted from what the business needs is
visible rather than silent. And **approval re-checks the allow-list**, because otherwise an editor who changes
an action's type walks straight past it.

**The number.** Every adversarial action blocked: a type policy doesn't list, a type not allowed for *this*
anomaly, an unknown anomaly type, an action about an item not in the brief. Two layers refuse them — the
contract cannot parse an invented type, and the gate refuses a real type the policy doesn't permit here.

> **The sentence:** "Approving means 'do the thing policy permits', not 'do anything'."

### 9.2 Why LangGraph here and plain code in Project A

**What it is.** `build_pack → narrate → draft_actions → policy_gate → ⟪interrupt⟫ → execute → record`, compiled
with `interrupt_before=["execute"]` and a Postgres checkpointer (`B-010`).

**Why it exists.** A's pipeline is fixed and a function call expresses it perfectly. This one has a **pause that
must survive a process ending**: the brief goes out at 06:00 and somebody approves at 14:00 from a different
machine. Writing that by hand means writing serialisation, resumption and a step ledger — which is what
LangGraph already is. Smallest abstraction that fits, in both directions.

**The interrupt is a stop, not a flag.** `execute` is never asked whether approval happened; the graph cannot
reach it. The test that justifies the whole abstraction throws away the compiled graph, the saver and the
connection after the pause, then resumes from Postgres with a fresh set.

> **The sentence:** "If the pause didn't have to survive a restart, plain code would do — and I'd have used it,
> like I did in the other project."

### 9.3 A failed send never loses the brief

**What it is.** The tracker adapter turns failures into statuses: timeout or 5xx → `FAILED_RETRYABLE`, 4xx →
`FAILED`, an adapter bug → retryable rather than a lost approval (`B-011`). Delivery happens last and is caught
entirely (`B-013`).

**Why it exists.** The person approved at 14:00 and went home. A tracker being down is the system's problem.
And a missing brief is a *silent* failure, where a missing email is a noisy inconvenience — so the brief is
written, validated and recorded before anything is sent.

**A bug worth telling.** `ops.mock_jira.action_id` was a foreign key into `ops.actions`, and the mock failed
because `execute` runs before the record is written. The fix wasn't ordering: **a real tracker has no
referential integrity with our database.** It accepts a ticket whether or not we've finished writing our row.
Modelling it with a foreign key made the mock fail in a way the live adapter never could.

> **The sentence:** "The mock failed in a way the real thing couldn't — which told me my model of the real
> thing was wrong."

### 9.4 The audience comes from the key, never the request

**What it is.** `API_AUDIENCES` maps an API key to the reader it belongs to; a key may only touch its own
threads (`B-012`).

**Why it exists.** A request that could name its own audience makes the permission filter decorative, and
knowing the date would be permission. Roles split the verbs: an analyst may build, a viewer may read, **only an
owner may decide** — and reading never advances a run.

> **The sentence:** "You can't ask for somebody else's brief, because you don't get to say whose brief it is."

---

## Week 10 — Proving it, and running it

### 10.1 Ops questions are answered in SQL

**What it is.** Seven views, `/ops/summary`, and alerts **named after RUNBOOK sections** — `no-briefs`,
`narration-falling-back`, `approvals-piling-up`, `action-failed`, `action-retrying` (`B-014`).

**Why it exists.** The number an operator reads at 08:00, the number a test asserts and the number the RUNBOOK
quotes should be the *same* number. And an alert without a page is a pager without an answer.

**Found by deploying.** The live ops page showed cost `$0.0000` while model usage showed `$0.0029`: the cost
join keyed on the pack's audience, which stored the *role* while the run row stores the *user*. Only production
surfaced it.

> **The sentence:** "Every alert it can raise has a page with the same name and a first step that's a command."

### 10.2 The report generates itself, including what failed

**What it is.** `make eval` produces `evals/REPORT.md` from four sections — detection, narration through the
real model, the policy gate under adversarial input, and the eight failure cases run as a subprocess (`B-015`).

**Why it exists.** A number in a report that nobody can reproduce with one command is marketing. Running the
failure cases as a subprocess means the report cannot claim what the tests do not.

**CI runs what it can pay for:** lint, the full suite against real Postgres, and the *deterministic* half of the
evaluation strictly, with no model configured. The narration numbers come from a machine with a key, and the
report says so.

**The number.** 261 tests, lint and mypy clean across 85 files, all targets met.

> **The sentence:** "The report has the caveats in it. Precision counts every unexplained HIGH as wrong, so 80%
> is a floor — and the half I never tuned on scores 100%."

---

## What this project is really about

1. **Code computes; the model narrates.** No LLM call exists in `metrics/` or `detect/`, and a test enforces it.
2. **Every claim cites a metric**, and every number is checked against the pack that produced it.
3. **Policy lives in a file the business owns**, enforced in code, never widened from a prompt.
4. **A person approves before anything acts** — enforced by a graph that cannot reach the node that acts.
5. **The brief always ships.** A missing brief is a silent failure.

**Live:** API `app-production-d624.up.railway.app`, console `console-production-c572.up.railway.app`, a cron
service, and Postgres holding gold, metrics and the ops record.

> **The closing sentence:** "One foundation, two uses. A proves the numbers are trustworthy; B acts on them and
> proves every claim is traceable. Both are honest about what they can't do."
