-- Constants the metric SQL needs, loaded from policy.yaml by metrics/runner.py before the files run.
-- A view body cannot take a query parameter, and interpolating numbers into SQL text would put the policy in
-- two places. The policy file stays the source; this table is how SQL reads it.
CREATE TABLE metrics.constants (
    name        text PRIMARY KEY,
    value       double precision NOT NULL,
    loaded_at   timestamptz      NOT NULL DEFAULT now()
);
COMMENT ON TABLE metrics.constants IS 'Mirror of policy.yaml metrics.*; never edited by hand.';
