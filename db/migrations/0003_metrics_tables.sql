-- Daily metric tables (plan B1). One table per grain, because a detector runs on a series at the grain an
-- anomaly can live at: total, plant, lane (plant × customer), plant × product group, yield line, location.
-- Every table is keyed by date plus its segment, so metrics/sql can merge a day idempotently.

CREATE TABLE metrics.daily_otif_total (
    metric_date        date PRIMARY KEY,
    lines              integer          NOT NULL,
    otif_rate          double precision,
    on_time_rate       double precision,
    fill_rate_count    double precision,
    fill_rate_weight   double precision,
    ordered_weight_lb  double precision,
    computed_at        timestamptz      NOT NULL DEFAULT now()
);

CREATE TABLE metrics.daily_otif_plant (
    metric_date        date NOT NULL,
    plant              text NOT NULL,
    lines              integer          NOT NULL,
    otif_rate          double precision,
    on_time_rate       double precision,
    fill_rate_count    double precision,
    fill_rate_weight   double precision,
    ordered_weight_lb  double precision,
    computed_at        timestamptz      NOT NULL DEFAULT now(),
    PRIMARY KEY (metric_date, plant)
);

CREATE TABLE metrics.daily_otif_lane (
    metric_date        date NOT NULL,
    plant              text NOT NULL,
    customer_no        text NOT NULL,
    lines              integer          NOT NULL,
    otif_rate          double precision,
    on_time_rate       double precision,
    fill_rate_count    double precision,
    fill_rate_weight   double precision,
    ordered_weight_lb  double precision,
    computed_at        timestamptz      NOT NULL DEFAULT now(),
    PRIMARY KEY (metric_date, plant, customer_no)
);

CREATE TABLE metrics.daily_fill_plant_group (
    metric_date        date NOT NULL,
    plant              text NOT NULL,
    product_group      text NOT NULL,
    lines              integer          NOT NULL,
    otif_rate          double precision,
    on_time_rate       double precision,
    fill_rate_count    double precision,
    fill_rate_weight   double precision,
    ordered_weight_lb  double precision,
    computed_at        timestamptz      NOT NULL DEFAULT now(),
    PRIMARY KEY (metric_date, plant, product_group)
);

-- Lines ordered on or before the date that had not shipped by it: the queue a plant is carrying that day.
CREATE TABLE metrics.daily_backlog_plant (
    metric_date         date NOT NULL,
    plant               text NOT NULL,
    open_backlog_lines  integer     NOT NULL,
    computed_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (metric_date, plant)
);

CREATE TABLE metrics.daily_yield (
    metric_date         date NOT NULL,
    plant               text NOT NULL,
    line                text NOT NULL,
    product_group       text NOT NULL,
    input_lb            double precision,
    output_lb           double precision,
    std_yield_pct       double precision,
    actual_yield_pct    double precision,
    yield_variance_pct  double precision,
    computed_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (metric_date, plant, line, product_group)
);

CREATE TABLE metrics.daily_inventory_location (
    metric_date           date NOT NULL,
    location              text NOT NULL,
    lots                  integer,
    on_hand_lb            double precision,
    inventory_age_days    double precision,   -- weighted mean age, the series A4 lives on
    max_age_days          integer,
    lb_at_risk_within_5d  double precision,   -- pounds whose lots expire within five days of the metric date
    computed_at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (metric_date, location)
);

-- What the last metrics run covered: the freshness the brief must disclose rather than narrate over.
CREATE TABLE metrics.runs (
    run_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    gold_source  text        NOT NULL,
    from_date    date,
    to_date      date,
    tables       jsonb       NOT NULL DEFAULT '{}'::jsonb,
    error        text
);
