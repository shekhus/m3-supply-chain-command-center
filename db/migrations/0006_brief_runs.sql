-- What a brief was, what it proposed, and what a person decided (plan B9).
--
-- The graph's own checkpoint tables live in `graph_checkpoints`, created here and then owned entirely by
-- langgraph's PostgresSaver: it manages their shape across versions, so nothing in this repo writes them.
CREATE SCHEMA IF NOT EXISTS graph_checkpoints;
--
-- The LangGraph checkpointer keeps its own tables in the `graph_checkpoints` schema and owns the *paused
-- machine*: enough to resume a run mid-flight. These tables are the durable business record — what went out,
-- who approved it, what it became — and they outlive any change to the graph's internals.

-- The pack and the brief as they were on the day. Kept whole, because a number in a ticket is only checkable
-- against the evidence that produced it, and that evidence is recomputed differently the moment policy moves.
CREATE TABLE ops.briefs (
    brief_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id       bigint      NOT NULL REFERENCES ops.runs (run_id) ON DELETE CASCADE,
    thread_id    text        NOT NULL,
    run_date     date        NOT NULL,
    audience     text        NOT NULL,
    pack         jsonb       NOT NULL,
    brief        jsonb       NOT NULL,
    narrated_by  text        NOT NULL,       -- llm | llm_retry | fallback
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (thread_id)
);

-- One row per proposed action, including the ones policy refused: a blocked action nobody can see is a policy
-- drifting in the dark.
CREATE TABLE ops.actions (
    action_id      text        PRIMARY KEY,
    run_id         bigint      NOT NULL REFERENCES ops.runs (run_id) ON DELETE CASCADE,
    thread_id      text        NOT NULL,
    run_date       date        NOT NULL,
    item_id        text        NOT NULL,
    anomaly_type   text        NOT NULL,
    segment_label  text        NOT NULL,
    metric         text        NOT NULL,
    type           text        NOT NULL,
    title          text        NOT NULL,
    body           text        NOT NULL,
    assignee_hint  text,
    evidence_refs  jsonb       NOT NULL DEFAULT '[]'::jsonb,
    status         text        NOT NULL,
    blocked_reason text,
    external_ref   text,
    decided_by     text,
    decided_at     timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX actions_run_idx ON ops.actions (run_date DESC, status);
CREATE INDEX actions_open_idx ON ops.actions (anomaly_type, metric, segment_label, created_at DESC);

-- Every decision a person made, kept separately from the action's current state: the audit answer to "who
-- approved this, and what did they change before they did?" survives the action being updated afterwards.
CREATE TABLE ops.decisions (
    decision_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    action_id   text        NOT NULL REFERENCES ops.actions (action_id) ON DELETE CASCADE,
    verdict     text        NOT NULL,        -- approve | edit | reject
    decided_by  text        NOT NULL,
    note        text,
    title       text,                        -- what they changed it to, when they edited
    body        text,
    decided_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX decisions_action_idx ON ops.decisions (action_id, decided_at DESC);

-- Tickets the mock Jira adapter "created", so a demo has somewhere to point (JIRA_MODE=mock).
--
-- `action_id` is deliberately NOT a foreign key. This table stands in for a system outside this database,
-- and a real tracker has no referential integrity with our rows: it accepts a ticket whether or not we have
-- finished writing our own record, and it keeps that ticket if we delete ours. Modelling it with a foreign
-- key made the mock fail in a way the live adapter never could (docs/decisions.md B-011).
CREATE TABLE ops.mock_jira (
    key         text        PRIMARY KEY,
    action_id   text,
    summary     text        NOT NULL,
    description text        NOT NULL,
    assignee    text,
    created_at  timestamptz NOT NULL DEFAULT now()
);
