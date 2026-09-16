-- Yield variance in percentage points against the item master's standard yield: A3's series.
INSERT INTO metrics.daily_yield AS t
    (metric_date, plant, line, product_group, input_lb, output_lb, std_yield_pct, actual_yield_pct,
     yield_variance_pct)
SELECT run_date, plant, line, product_group,
       sum(input_lb), sum(output_lb), avg(std_yield_pct), avg(actual_yield_pct), avg(yield_variance_pct)
FROM gold.fact_yield
WHERE run_date BETWEEN :from_date AND :to_date
GROUP BY run_date, plant, line, product_group
ON CONFLICT (metric_date, plant, line, product_group) DO UPDATE SET
    input_lb = EXCLUDED.input_lb, output_lb = EXCLUDED.output_lb, std_yield_pct = EXCLUDED.std_yield_pct,
    actual_yield_pct = EXCLUDED.actual_yield_pct, yield_variance_pct = EXCLUDED.yield_variance_pct,
    computed_at = now();
