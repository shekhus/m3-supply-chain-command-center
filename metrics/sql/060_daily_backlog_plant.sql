-- Open backlog: lines ordered on or before the date that had not shipped by it. One row per plant per date in
-- the window, including zero-backlog days, so a detector sees a continuous series rather than gaps.
INSERT INTO metrics.daily_backlog_plant AS t (metric_date, plant, open_backlog_lines)
SELECT d.metric_date, p.plant,
       count(f.order_no) FILTER (WHERE f.order_date <= d.metric_date AND f.metric_date > d.metric_date)
FROM (SELECT generate_series(CAST(:from_date AS date), CAST(:to_date AS date), interval '1 day')::date AS metric_date) d
CROSS JOIN (SELECT DISTINCT plant FROM metrics.v_delivery_flags) p
LEFT JOIN metrics.v_delivery_flags f
       ON f.plant = p.plant AND f.order_date <= d.metric_date AND f.metric_date > d.metric_date
GROUP BY d.metric_date, p.plant
ON CONFLICT (metric_date, plant) DO UPDATE SET
    open_backlog_lines = EXCLUDED.open_backlog_lines, computed_at = now();
