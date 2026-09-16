-- Inventory ageing per location: A4's series, plus the pounds close to expiry that policy thresholds on.
-- Age is weighted by pounds on hand, not a plain mean of lot ages: a plain mean lets one large stalled lot
-- hide behind many small fast-moving ones, and "my inventory is getting old" is a question about pounds.
INSERT INTO metrics.daily_inventory_location AS t
    (metric_date, location, lots, on_hand_lb, inventory_age_days, max_age_days, lb_at_risk_within_5d)
SELECT snapshot_date, location,
       count(DISTINCT lot_no),
       sum(on_hand_lb),
       CASE WHEN sum(on_hand_lb) > 0 THEN sum(age_days * on_hand_lb) / sum(on_hand_lb) END,
       max(age_days),
       sum(CASE WHEN days_to_expiry <= (SELECT value FROM metrics.constants WHERE name = 'at_risk_days') THEN on_hand_lb ELSE 0 END)
FROM gold.fact_inventory
WHERE snapshot_date BETWEEN :from_date AND :to_date
GROUP BY snapshot_date, location
ON CONFLICT (metric_date, location) DO UPDATE SET
    lots = EXCLUDED.lots, on_hand_lb = EXCLUDED.on_hand_lb,
    inventory_age_days = EXCLUDED.inventory_age_days, max_age_days = EXCLUDED.max_age_days,
    lb_at_risk_within_5d = EXCLUDED.lb_at_risk_within_5d, computed_at = now();
