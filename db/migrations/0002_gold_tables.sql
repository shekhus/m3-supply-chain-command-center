-- Gold this system reads (docs/plan.md §3.3). Standalone mode loads these from synth/; integrated mode gets
-- the same shapes from m3-trusted-data-foundation. Nothing here is written by narration or by an action.
--
-- The generator's `_anomaly_id` marker column is deliberately NOT part of gold: it is answer-key material, and
-- a detector able to read it would score itself (docs/decisions.md B-003).

CREATE TABLE gold.dim_customer (
    customer_no    text PRIMARY KEY,
    customer_name  text NOT NULL,
    city           text,
    state          text,
    channel        text,
    volume_share   double precision
);

CREATE TABLE gold.dim_item (
    item_no            text PRIMARY KEY,
    item_name          text NOT NULL,
    product_group      text NOT NULL,
    catch_weight_flag  boolean NOT NULL,
    shelf_life_days    integer,
    std_yield_pct      double precision,
    avg_lb_per_case    double precision,
    uom                text,
    volume_share       double precision
);

-- One row per shipped order line. on_time / in_full / otif are the governed flags as published; the metric
-- layer recomputes them from the raw fields and a test asserts the two agree (B-003).
CREATE TABLE gold.fact_delivery (
    order_no                 text    NOT NULL,
    line_no                  integer NOT NULL,
    customer_no              text    NOT NULL,
    item_no                  text    NOT NULL,
    plant                    text    NOT NULL,
    product_group            text,
    catch_weight_flag        boolean,
    order_date               date,
    requested_date           date,
    confirmed_delivery_date  date,
    issue_date               date,
    ordered_qty              double precision,
    invoiced_qty             double precision,
    ordered_weight_lb        double precision,
    invoiced_weight_lb       double precision,
    uom                      text,
    lot_no                   text,
    production_date          date,
    expiry_date              date,
    customer_po              text,
    order_type               text,
    customer_name            text,
    city                     text,
    state                    text,
    channel                  text,
    lane                     text,
    on_time                  integer,
    in_full                  integer,
    otif                     integer,
    late_days                integer,
    PRIMARY KEY (order_no, line_no)
);
CREATE INDEX fact_delivery_issue_idx ON gold.fact_delivery (issue_date);
CREATE INDEX fact_delivery_plant_idx ON gold.fact_delivery (plant, issue_date);
CREATE INDEX fact_delivery_lane_idx ON gold.fact_delivery (plant, customer_no, issue_date);

CREATE TABLE gold.fact_inventory (
    snapshot_date    date    NOT NULL,
    location         text    NOT NULL,
    item_no          text    NOT NULL,
    lot_no           text    NOT NULL,
    on_hand_lb       double precision,
    production_date  date,
    expiry_date      date,
    age_days         integer,
    days_to_expiry   integer,
    PRIMARY KEY (snapshot_date, location, lot_no)
);
CREATE INDEX fact_inventory_date_idx ON gold.fact_inventory (snapshot_date);

CREATE TABLE gold.fact_yield (
    run_date           date NOT NULL,
    plant              text NOT NULL,
    line               text NOT NULL,
    product_group      text NOT NULL,
    input_lb           double precision,
    output_lb          double precision,
    std_yield_pct      double precision,
    actual_yield_pct   double precision,
    yield_variance_pct double precision,
    PRIMARY KEY (run_date, plant, line, product_group)
);
CREATE INDEX fact_yield_date_idx ON gold.fact_yield (run_date);
