-- What the system did, and what it cost (plan B12). Written by every run, read by the ops page in week 10.
-- Separate from `metrics` on purpose: these are records of this system's behaviour, not measurements of the
-- business, and nothing here is ever an input to a brief.

-- One row per brief. `status` follows the approval flow in week 9.
CREATE TABLE ops.runs (
    run_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_date     date        NOT NULL,
    audience     text        NOT NULL,
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    status       text        NOT NULL DEFAULT 'RUNNING',
    items        integer     NOT NULL DEFAULT 0,
    narrated_by  text,                      -- 'llm', 'llm_retry' or 'fallback': the fallback rate, honestly
    fallback_reason text,
    error        text,
    UNIQUE (run_date, audience)             -- a second run for the same day and reader returns the first
);

-- Every model call, whether it worked or not. A call that is not recorded did not happen, as far as the
-- cost and latency numbers in the report are concerned.
CREATE TABLE ops.llm_calls (
    call_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at            timestamptz NOT NULL DEFAULT now(),
    batch_id      text,
    purpose       text        NOT NULL,
    provider      text        NOT NULL,
    model         text        NOT NULL,
    prompt_hash   text        NOT NULL,
    input_tokens  integer,
    output_tokens integer,
    latency_ms    integer     NOT NULL,
    cost_usd      double precision,
    outcome       text        NOT NULL,     -- ok | invalid_output | fallback | error
    error         text
);
CREATE INDEX llm_calls_at_idx ON ops.llm_calls (at DESC);
CREATE INDEX llm_calls_purpose_idx ON ops.llm_calls (purpose, outcome);

-- Tool and adapter calls (the Jira adapter in week 9), including the ones that timed out.
CREATE TABLE ops.tool_calls (
    tool_call_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at           timestamptz NOT NULL DEFAULT now(),
    run_id       bigint REFERENCES ops.runs (run_id),
    tool         text        NOT NULL,
    arguments    jsonb       NOT NULL DEFAULT '{}'::jsonb,
    outcome      text        NOT NULL,
    latency_ms   integer,
    error        text
);
