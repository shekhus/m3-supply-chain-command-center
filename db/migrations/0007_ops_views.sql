-- The ops page's questions, answered in SQL (plan B12).
--
-- Views rather than queries buried in a Streamlit page: the numbers an operator reads at 08:00 are the same
-- numbers a test asserts and a RUNBOOK quotes, and none of them is a pandas expression somebody rewrote.

-- What each morning cost and how it went. One row per run.
CREATE VIEW ops.v_run_health AS
SELECT r.run_date,
       r.audience,
       r.status,
       r.items,
       r.narrated_by,
       r.fallback_reason,
       EXTRACT(EPOCH FROM (r.finished_at - r.started_at))::int AS seconds,
       (SELECT count(*) FROM ops.actions a WHERE a.run_id = r.run_id)                        AS actions,
       (SELECT count(*) FROM ops.actions a WHERE a.run_id = r.run_id AND a.status = 'EXECUTED') AS executed,
       (SELECT count(*) FROM ops.actions a WHERE a.run_id = r.run_id AND a.status = 'BLOCKED')  AS blocked,
       (SELECT coalesce(sum(c.cost_usd), 0) FROM ops.llm_calls c
         WHERE c.batch_id = 'brief-' || r.run_date::text || '-' || r.audience)                AS cost_usd
FROM ops.runs r;

-- Model usage and what it cost, by day. The honest answer to "what does this run on?"
CREATE VIEW ops.v_model_usage AS
SELECT date_trunc('day', at)::date       AS day,
       provider,
       model,
       purpose,
       count(*)                          AS calls,
       count(*) FILTER (WHERE outcome = 'ok')             AS ok,
       count(*) FILTER (WHERE outcome = 'invalid_output') AS invalid,
       count(*) FILTER (WHERE outcome = 'error')          AS errors,
       coalesce(sum(input_tokens), 0)    AS tokens_in,
       coalesce(sum(output_tokens), 0)   AS tokens_out,
       round(coalesce(sum(cost_usd), 0)::numeric, 4)      AS cost_usd,
       percentile_disc(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_latency_ms
FROM ops.llm_calls
GROUP BY 1, 2, 3, 4;

-- How often the model's answer was good enough to ship, and how often the template had to. A fallback rate
-- nobody publishes quietly becomes 100%.
CREATE VIEW ops.v_narration_health AS
SELECT run_date,
       count(*)                                                AS briefs,
       count(*) FILTER (WHERE narrated_by = 'llm')             AS first_pass,
       count(*) FILTER (WHERE narrated_by = 'llm_retry')       AS corrected,
       count(*) FILTER (WHERE narrated_by = 'fallback')        AS fell_back,
       round(100.0 * count(*) FILTER (WHERE narrated_by = 'fallback') / nullif(count(*), 0), 1)
                                                               AS fallback_pct
FROM ops.runs
WHERE narrated_by IS NOT NULL
GROUP BY 1;

-- What is waiting for a person, and for how long. The queue that matters most: an approval nobody gave is a
-- brief nobody read.
CREATE VIEW ops.v_pending_approvals AS
SELECT a.run_date,
       a.thread_id,
       a.action_id,
       a.type,
       a.title,
       a.assignee_hint,
       (CURRENT_DATE - a.run_date)     AS days_waiting
FROM ops.actions a
WHERE a.status = 'PROPOSED';

-- Actions whose tracker call failed in a way the next run should retry, and the ones that need a person.
CREATE VIEW ops.v_action_failures AS
SELECT a.run_date, a.action_id, a.type, a.title, a.status, a.external_ref,
       (SELECT c.error FROM ops.tool_calls c
         WHERE c.arguments->>'action_id' = a.action_id ORDER BY c.at DESC LIMIT 1) AS last_error
FROM ops.actions a
WHERE a.status IN ('FAILED_RETRYABLE', 'FAILED');

-- Everything policy refused, so a gate that has drifted from what the business needs is visible rather than
-- silent.
CREATE VIEW ops.v_policy_refusals AS
SELECT run_date, anomaly_type, type, status, blocked_reason, count(*) AS times
FROM ops.actions
WHERE status IN ('BLOCKED', 'SUPPRESSED')
GROUP BY 1, 2, 3, 4, 5;

-- Delivery attempts, including the ones that failed: the brief existing is not the same as it arriving.
CREATE VIEW ops.v_delivery AS
SELECT date_trunc('day', at)::date AS day,
       replace(tool, 'deliver:', '') AS channel,
       outcome,
       count(*) AS attempts
FROM ops.tool_calls
WHERE tool LIKE 'deliver:%'
GROUP BY 1, 2, 3;
