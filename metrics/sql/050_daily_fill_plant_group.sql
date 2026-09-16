-- Plant × product group: A2 is a weight-basis shortfall that the count basis cannot see, so both fill rates
-- are carried at this grain.
INSERT INTO metrics.daily_fill_plant_group AS t
    (metric_date, plant, product_group, lines, otif_rate, on_time_rate, fill_rate_count, fill_rate_weight,
     ordered_weight_lb)
SELECT metric_date, plant, product_group,
       count(*),
       avg(otif::double precision),
       avg(on_time::double precision),
       CASE WHEN sum(ordered_qty) > 0 THEN sum(invoiced_qty) / sum(ordered_qty) END,
       CASE WHEN sum(ordered_weight_lb) > 0
            THEN sum(invoiced_weight_lb) / sum(ordered_weight_lb) END,
       sum(ordered_weight_lb)
FROM metrics.v_delivery_flags
WHERE metric_date BETWEEN :from_date AND :to_date
GROUP BY metric_date, plant, product_group
ON CONFLICT (metric_date, plant, product_group) DO UPDATE SET
    lines = EXCLUDED.lines, otif_rate = EXCLUDED.otif_rate, on_time_rate = EXCLUDED.on_time_rate,
    fill_rate_count = EXCLUDED.fill_rate_count, fill_rate_weight = EXCLUDED.fill_rate_weight,
    ordered_weight_lb = EXCLUDED.ordered_weight_lb, computed_at = now();
