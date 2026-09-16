-- Delivery flags, recomputed from the raw fields rather than trusting gold's published flags.
-- Definition (mirrors the governed metric dictionary in m3-trusted-data-foundation, otif v3):
--   on time  : issue_date <= confirmed_delivery_date
--   in full  : catch-weight items by weight with a 2% tolerance; everything else by case count
--   otif     : on time AND in full
-- tests/test_metrics_sql.py asserts these agree with gold.on_time / in_full / otif row for row: the two
-- projects implement one definition, and this proves it rather than assuming it.
CREATE OR REPLACE VIEW metrics.v_delivery_flags AS
SELECT
    d.order_no,
    d.line_no,
    d.issue_date AS metric_date,
    d.order_date,
    d.plant,
    d.customer_no,
    d.product_group,
    d.ordered_qty,
    d.invoiced_qty,
    d.ordered_weight_lb,
    d.invoiced_weight_lb,
    (d.issue_date <= d.confirmed_delivery_date)::int AS on_time,
    (CASE
        WHEN d.catch_weight_flag
            THEN d.invoiced_weight_lb >= d.ordered_weight_lb * (1 - (SELECT value FROM metrics.constants WHERE name = 'weight_tolerance'))
        ELSE d.invoiced_qty >= d.ordered_qty
     END)::int AS in_full,
    ((d.issue_date <= d.confirmed_delivery_date) AND (CASE
        WHEN d.catch_weight_flag
            THEN d.invoiced_weight_lb >= d.ordered_weight_lb * (1 - (SELECT value FROM metrics.constants WHERE name = 'weight_tolerance'))
        ELSE d.invoiced_qty >= d.ordered_qty
     END))::int AS otif
FROM gold.fact_delivery d
WHERE d.issue_date IS NOT NULL;
