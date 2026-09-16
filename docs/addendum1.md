# Addendum 1 — Agentic Flow and Retrieval

**Supplements:** `docs/plan.md`
**Adds:** Feature A13 (Project A), Features B15–B17 (Project B), and the
conventions both `CLAUDE.md` files now carry.
**Date:** 12 September 2026

---

## 0. Why this addendum exists

Two honest gaps in the original plan:

1. **Project A had human-in-the-loop but no agent.** Its AI was one LLM call with
   a Pydantic contract — no tools, no loop, no state. That was a defensible
   choice, but it left the portfolio with agentic depth in only one repo, which
   is a real weakness when you are also pursuing Agentic AI Developer roles.

2. **Neither project had retrieval.** I claimed Playbook Project 1 was "woven
   into" Project A; only its *permissions* were. There was no RAG anywhere —
   the single most-asked FDE interview topic, and the one thing the workshop
   syllabus lists that the projects could not demonstrate.

Both are closed here, and closed in a way that is **justified by the problem**
rather than bolted on to tick a box. The test applied throughout: *would a
competent engineer build this, or is it here to look sophisticated?*

---

## 1. What must NOT become agentic

State this in interviews before anything else. It is the stronger half of the
answer.

| Component | Stays a plain call | Why |
|---|---|---|
| A's mapping proposer | Yes | One well-scoped call: profile in, mapping proposal out. Wrapping it in a graph adds latency, cost, and failure modes for nothing. |
| A's validation rules | Yes — no LLM at all | Deterministic rules in code. A rule that "usually" fires is not a rule. |
| B's metric layer | Yes — no LLM at all | Code calculates. Always. |
| B's detectors | Yes — no LLM at all | Statistical checks, reproducible, replayable without API cost. |

**The justification test**, recorded in both `CLAUDE.md` files:

> A component may become agentic only if **the number of steps is not knowable
> in advance**. If you can draw the flowchart, write the flowchart. Needing a
> loop because the next action depends on what the last tool returned is a
> reason. Wanting it to look impressive is not.

Keeping the mapper deliberately non-agentic alongside a genuinely agentic
triager is a stronger interview position than two agents, because it shows the
choice was made rather than defaulted to.

---

## 2. Feature A13 — Exception Resolution Agent

### 2.1 The problem it solves

An exception lands in the queue: *customer_no `CUST-0044` not found in master*.

What a human analyst does next is not a fixed sequence. They check whether the
value matches under a different format. They check whether a near-identical
exception was resolved last month. They look at the raw source rows to see
whether the file itself is malformed. They check whether the same key appears in
another plant's extract. **How many of those steps they take, and in what order,
depends on what each one returns.** That is the definition of a task that needs
an agent rather than a pipeline.

It is also Playbook **Project 2 (intake-to-resolution)**, which the original plan
claimed was woven into A but was not.

### 2.2 The four outcomes

The agent must classify every exception into exactly one:

| Outcome | Meaning | Example from the dataset |
|---|---|---|
| `auto_fixable` | A known normalisation, applied with the original retained | `CUST-0044` is Sheldon's format for `C000044`; trailing whitespace on an item number |
| `needs_master_data` | Genuinely absent from a master; a person must create it | `C9xxxxx` customer that was never set up |
| `source_defect` | The source is wrong or incomplete; no fix is possible here | Hastings weights in kg; Cedar Falls lot numbers absent before June 2026 |
| `escalate` | Not a data problem at all | Lot shipped after expiry (compliance); negative quantity (a credit); catch-weight overage (commercial) |

### 2.3 Tools

```python
search_prior_resolutions(symptom: str, rule_id: str | None) -> list[Resolution]
    # semantic search over kb/resolutions/prior_resolutions.json (93 records).
    # The agent's memory. This is the retrieval surface that closes the RAG gap.

search_knowledge_base(query: str, access: AccessContext) -> list[Chunk]
    # the SOPs, source notes, metric definitions, correction memos.
    # Permission-filtered BEFORE the call, never after.

lookup_master(value: str, master: str, fuzzy: bool = True) -> list[Match]
    # deterministic. Tries exact, then each known normalisation, then fuzzy.

inspect_source_rows(source: str, row_key: str, window: int = 5) -> list[Row]
    # the raw rows around the failing one, to distinguish a bad row from a
    # bad file.

check_other_sources(key: str, field: str) -> dict[str, list[Row]]
    # does this key appear, correctly, in another plant's extract?
```

Every tool returns a Pydantic model. Every call is logged to `ops.tool_calls`
with latency and outcome.

### 2.4 The graph

```
  triage_exception
        │
        ▼
  ┌─────────────────┐
  │  investigate    │◄──┐   loop: the agent chooses the next tool based on what
  │  (tool loop)    │   │   the last one returned; max 6 iterations, then it
  └────────┬────────┘───┘   must classify with what it has
           ▼
     classify  ──────────────► Pydantic Resolution contract
           │                   {outcome, confidence, evidence_refs[],
           ▼                    proposed_fix | None, rationale}
     policy_gate  (code)  ────► rejects any proposed_fix outside the
           │                    SOP-DQ-001-v2 auto-fix allowlist
           ▼
     [interrupt_before: apply]
           ▼
     apply  ──► updates ops.exceptions, writes a new resolution record
           ▼
     record
```

**Non-negotiables, all enforced in code, not prompts:**

- **The auto-fix allowlist comes from the SOP, not the model.** `SOP-DQ-001-v2`
  permits exactly two auto-fixes: unit-of-measure normalisation and date-format
  normalisation, both with the original value retained. It explicitly forbids
  any automated change to a customer number, item number, quantity, or weight.
  The policy gate encodes that list. An agent proposing `customer_no: CUST-0044
  → C000044` as an *auto-fix* is **rejected by the gate** even though the
  mapping is correct — it goes to a human as a proposal. This is the sharpest
  guardrail test in either project, because the agent is *right* and still
  blocked.
- **Max 6 tool iterations**, then classify with what it has. An agent that
  cannot conclude must say so, not loop.
- **`interrupt_before=["apply"]`** with the Postgres checkpointer. Nothing
  changes without an `owner` approving.
- **Every classification cites its evidence** — resolution ids, document ids,
  or row keys. A classification with no `evidence_refs` fails the contract.

### 2.5 Evaluation

The dataset already supports all of it. 580 seeded faults across 9 rules, each
with a known row key and rule id.

| Measure | Method | Target |
|---|---|---|
| Classification accuracy | Agent outcome vs the expected outcome per rule pattern | ≥ 85% overall |
| Per-rule accuracy | Broken out by rule id | Report all 9; no rule below 70% |
| **Coverage honesty** | On the 17 V006 faults on Cedar Falls rows before June 2026, the agent must return `source_defect` with "not covered" — **not** a fix, and **not** a compliance escalation | **100%** — this is a hard gate |
| Evidence quality | Share of classifications citing a retrieved resolution or document | ≥ 90% |
| Policy gate | Adversarial: inject proposed fixes outside the allowlist | 100% blocked |
| Tool efficiency | Mean tool calls per exception; share hitting the iteration cap | Report |
| Escalation precision | False escalations on auto-fixable patterns | ≤ 5% |

**The coverage-honesty gate deserves emphasis.** Those 17 faults are
structurally undetectable — Cedar Falls sent no `lot_no` before June 2026. An
agent that invents a fix is worse than useless, and one that escalates them as
compliance findings will flood quality with noise. The only correct answer is
"the source does not carry this field for this period; report as not covered."
`SRC-PLT01-001` says exactly that, so a correctly retrieving agent can find it.

### 2.6 Build slot

**Week 5–6**, bridging the two projects. It needs A's exception queue (week 3)
and the retrieval layer (below), and it makes a natural warm-up for B's
LangGraph work.

---

## 3. Retrieval — the shared layer

Built once in Project A, imported by Project B. One implementation, two uses.

### 3.1 What is in the corpus

Generated by `synth/knowledge.py`, shipped in `data/kb/`:

- **26 documents** — SOPs, source system notes, the metric dictionary narrative,
  customer service agreements, product specifications, plant profiles, memos.
  **8 are distractors**: plausible, well-written, and never the right answer.
- **93 prior exception resolutions** across the four outcome classes.
- **53 golden questions** with expected and forbidden document ids, required
  answer content, refusal flags, and an asker role and plant.

### 3.2 The ten challenges, and which ones actually bite

`rag_probe.py` establishes a **TF-IDF lexical floor** and reports honestly:

| Challenge | Verdict | What it costs you if unhandled |
|---|---|---|
| **C3 access control** | **BITES HARD** | 4/4 unauthorised questions leaked a restricted document. Every one is a data leak. |
| **C5 refusal** | **BITES HARD** | Top-k always returns something. Unanswerable questions score 0.00–0.25, overlapping legitimate hits. |
| **C1 superseded versions** | **BITES** | 3/3 retrieved the superseded SOP alongside the current one. |
| **C9 synthesis** | **BITES** | Both multi-document questions came back incomplete at k=6 — and the missing document is the one that changes the answer. |
| **C8 near-duplicates** | **BITES** | 3/3, and dedup-by-type did not fix it. Needs a reranker. |
| C2 contradictory docs | Retrieval fine; **precedence** is the problem | Both documents come back; the system must know the memo wins. |
| C4 stale content | Retrieval fine; **scoping** is the problem | Expired ≠ useless. Right for its period, wrong for today. |
| C6 terminology | Does not bite at this corpus size | TF-IDF scores 4/4. **Do not claim embeddings earned this** — measure the lift and report it even if zero. |
| C7 tables / C10 long docs | Does not bite at retrieval | Chunking matters for answer precision and token cost, not recall. Measure those. |

**This table is the interview asset.** "Our RAG scores 94%" is noise. "Retrieval
recall is X against a lexical floor of Y, permission leakage is zero, refusal
rate on unanswerable questions is Z, and near-duplicates are the class still
failing" is an engineer talking.

### 3.3 Features

**Project A:**

| # | Feature | Detail |
|---|---|---|
| A14 | **Chunking** | Section-level on markdown headings. Tables never split from their header row. Frontmatter (`doc_id`, `version`, `status`, `access_level`, `plant`, `effective_date`) attached to every chunk. |
| A15 | **Hybrid retrieval** | Embeddings + BM25, reciprocal rank fusion. **TF-IDF baseline retained and reported alongside**, so the embedding lift is measured, not assumed. |
| A16 | **Pre-model access filter** | `AccessContext(role, plant)` applied to the candidate set *before* the model call. Over-restriction counts as a failure too — the matched-pair questions test both directions. |
| A17 | **Version and status precedence** | `status != current` excluded by default. A document declaring precedence over another (`MEMO-2025-11`) wins on the overlap. Historical questions may opt back in. |
| A18 | **Refusal gate** | Similarity floor **plus** an explicit "if the answer is not in the provided context, say so" instruction. The probe shows the threshold alone is insufficient. |

**Project B:**

| # | Feature | Detail |
|---|---|---|
| B15 | **Policy lookup in the brief** | Escalation tier and owner retrieved from `SOP-OPS-001` and the relevant SLA, cited in the brief item. |
| B16 | **Announced-event awareness** | Before escalating, search for an operational notice covering the window. `MEMO-2026-06` announces the July shutdown and states a 15–20 point drop is expected — so the brief **explains** A5 rather than merely declining to escalate it. |
| B17 | **Permission-scoped KB in the pack** | A plant manager's evidence pack carries only their plant's documents, matching the metric filtering already in B5. |

### 3.4 B16 is the best thing in either project

The A5 decoy now has a **three-layer** correct response:

1. **Detector** — day-of-week and holiday awareness stops it being flagged HIGH.
2. **Retrieval** — the shutdown memo is found for the window.
3. **Narration** — the brief says *"on-time performance fell 18 points from 2–6
   July. A shutdown across all facilities was announced for this window
   (MEMO-2026-06), which anticipated a 15–20 point drop. Per SOP-OPS-001 an
   announced event is not an escalation trigger. No action proposed."*

That is the difference between a dashboard and a colleague. It also exercises
detection, retrieval, grounding, citation, and policy in one event — and the
golden question `Q081` scores it directly.

---

## 4. Revised timeline

| Week | Project | Addition |
|---|---|---|
| 1–4 | A | unchanged |
| **5** | **A** | **Retrieval layer: A14–A18, scored against the 53 golden questions** |
| **6** | **A** | **A13 exception agent: tools, graph, policy gate, approval, evals** |
| 7–10 | B | as before, **plus B15–B17 in week 9** |
| 11 | both | demos, READMEs, public-profile review |

Project A grows from five weeks to six. If time is tight, **A13 is the item to
defer, not the retrieval layer** — retrieval closes the interview gap on its
own, and A13 depends on it.

---

## 5. What goes in the READMEs

Add to Project A's "How did you evaluate it?":

> Retrieval is measured per challenge, not as an aggregate. A TF-IDF baseline is
> reported alongside the embedding pipeline so the lift is visible. Five of the
> ten engineered challenges defeat a naive retriever; the other five are real
> problems that live in prompting and precedence rather than in retrieval
> recall, and are measured separately. Permission leakage is reported as an
> absolute count, because one leak is a failure regardless of the rate.

Add to the "What would you change before production?" of both:

> The corpus is 26 documents. At that size lexical retrieval is a strong
> baseline and several engineered challenges do not bite. At a realistic
> enterprise scale — thousands of documents, many near-duplicates, live
> permission changes — chunking strategy, reranking, and index freshness become
> the dominant concerns. I would re-run the per-challenge measurement at scale
> before assuming any of these results hold.
