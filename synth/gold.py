"""
Build the gold fact tables and the daily metric series.

Two jobs:

  1. Project B standalone mode needs gold tables without running Project A.
     `build_gold()` produces them directly from the clean canonical events.

  2. The generator must PROVE its seeded anomalies are actually visible in the
     metrics. `daily_metrics()` plus the self-check in generate.py does that.
     A generator that claims an anomaly it did not produce would silently
     invalidate every evaluation downstream.

The metric definitions here mirror metrics/otif.yaml (v3): catch-weight items
use the weight basis with a 2% tolerance, everything else uses case count.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import config as C

# ---------------------------------------------------------------------------
# gold
# ---------------------------------------------------------------------------

def build_gold(
    lines: pd.DataFrame,
    customers: pd.DataFrame,
    items: pd.DataFrame,
    inventory: pd.DataFrame,
    yields: pd.DataFrame,
) -> dict[str, pd.DataFrame]:
    """Star-ish schema: one fact per grain, plus conformed dimensions."""
    fact_delivery = lines.merge(
        customers[["customer_no", "customer_name", "city", "state", "channel"]],
        on="customer_no", how="left",
    ).copy()

    fact_delivery["lane"] = fact_delivery["plant"] + " > " + fact_delivery["customer_no"]

    on_time = (
        pd.to_datetime(fact_delivery["issue_date"])
        <= pd.to_datetime(fact_delivery["confirmed_delivery_date"])
    )

    in_full_count = fact_delivery["invoiced_qty"] >= fact_delivery["ordered_qty"]
    in_full_weight = fact_delivery["invoiced_weight_lb"] >= (
        fact_delivery["ordered_weight_lb"] * (1 - C.WEIGHT_TOLERANCE)
    )
    in_full = np.where(fact_delivery["catch_weight_flag"], in_full_weight, in_full_count)

    fact_delivery["on_time"] = on_time.astype(int)
    fact_delivery["in_full"] = pd.Series(in_full, index=fact_delivery.index).astype(int)
    fact_delivery["otif"] = (fact_delivery["on_time"] & fact_delivery["in_full"]).astype(int)
    fact_delivery["late_days"] = (
        pd.to_datetime(fact_delivery["issue_date"])
        - pd.to_datetime(fact_delivery["confirmed_delivery_date"])
    ).dt.days.clip(lower=0)

    fact_inventory = inventory.copy()
    fact_inventory["age_days"] = (
        pd.to_datetime(fact_inventory["snapshot_date"])
        - pd.to_datetime(fact_inventory["production_date"])
    ).dt.days
    fact_inventory["days_to_expiry"] = (
        pd.to_datetime(fact_inventory["expiry_date"])
        - pd.to_datetime(fact_inventory["snapshot_date"])
    ).dt.days

    fact_yield = yields.copy()
    fact_yield["actual_yield_pct"] = np.where(
        fact_yield["input_lb"] > 0, fact_yield["output_lb"] / fact_yield["input_lb"], np.nan
    )
    fact_yield["yield_variance_pct"] = (
        fact_yield["actual_yield_pct"] - fact_yield["std_yield_pct"]
    ) * 100.0

    return {
        "fact_delivery": fact_delivery,
        "fact_inventory": fact_inventory,
        "fact_yield": fact_yield,
        "dim_customer": customers.drop(columns=["volume_weight"], errors="ignore"),
        "dim_item": items.drop(columns=["volume_weight"], errors="ignore"),
    }


# ---------------------------------------------------------------------------
# daily metrics
# ---------------------------------------------------------------------------

def daily_metrics(gold: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """
    The daily_* series Project B's detectors run on. Grains chosen so that each
    seeded anomaly is expressible at the grain it was planted at.
    """
    fd = gold["fact_delivery"]
    fd = fd.assign(metric_date=pd.to_datetime(fd["issue_date"]).dt.date)

    def rate(g: pd.DataFrame) -> pd.Series:
        return pd.Series(
            {
                "lines": len(g),
                "otif_rate": g["otif"].mean(),
                "on_time_rate": g["on_time"].mean(),
                "fill_rate_count": (g["invoiced_qty"].sum() / g["ordered_qty"].sum())
                if g["ordered_qty"].sum() else np.nan,
                "fill_rate_weight": (
                    g["invoiced_weight_lb"].sum() / g["ordered_weight_lb"].sum()
                ) if g["ordered_weight_lb"].sum() else np.nan,
                "ordered_weight_lb": g["ordered_weight_lb"].sum(),
            }
        )

    out: dict[str, pd.DataFrame] = {}

    out["daily_otif_total"] = (
        fd.groupby("metric_date").apply(rate, include_groups=False).reset_index()
    )
    out["daily_otif_plant"] = (
        fd.groupby(["metric_date", "plant"]).apply(rate, include_groups=False).reset_index()
    )
    out["daily_otif_lane"] = (
        fd.groupby(["metric_date", "plant", "customer_no"])
        .apply(rate, include_groups=False).reset_index()
    )
    out["daily_fill_plant_group"] = (
        fd.groupby(["metric_date", "plant", "product_group"])
        .apply(rate, include_groups=False).reset_index()
    )

    # open backlog: lines ordered but not yet issued, as at each date
    orders = fd.assign(order_date=pd.to_datetime(fd["order_date"]).dt.date)
    dates = sorted(orders["metric_date"].unique())
    backlog_rows = []
    for plant, grp in orders.groupby("plant"):
        od = grp["order_date"].to_numpy()
        idt = grp["metric_date"].to_numpy()
        for d in dates:
            backlog_rows.append(
                {"metric_date": d, "plant": plant,
                 "open_backlog_lines": int(((od <= d) & (idt > d)).sum())}
            )
    out["daily_backlog_plant"] = pd.DataFrame(backlog_rows)

    fy = gold["fact_yield"]
    out["daily_yield"] = (
        fy.groupby(["run_date", "plant", "line", "product_group"])
        .agg(
            input_lb=("input_lb", "sum"),
            output_lb=("output_lb", "sum"),
            std_yield_pct=("std_yield_pct", "mean"),
            yield_variance_pct=("yield_variance_pct", "mean"),
        )
        .reset_index()
        .rename(columns={"run_date": "metric_date"})
    )

    fi = gold["fact_inventory"].copy()
    # Weight-weighted ageing, not a plain mean of lot ages. A plain mean lets a
    # large stalled lot hide behind many small fast-moving ones; weighting by
    # pounds on hand is both more sensitive and closer to what a planner means
    # by "my inventory is getting old".
    fi["age_lb"] = fi["age_days"] * fi["on_hand_lb"]
    fi["at_risk_lb"] = fi["on_hand_lb"].where(fi["days_to_expiry"] <= 5, 0.0)

    inv = (
        fi.groupby(["snapshot_date", "location"])
        .agg(
            on_hand_lb=("on_hand_lb", "sum"),
            age_lb=("age_lb", "sum"),
            mean_age_days=("age_days", "mean"),
            max_age_days=("age_days", "max"),
            lb_at_risk_within_5d=("at_risk_lb", "sum"),
            lots=("lot_no", "nunique"),
        )
        .reset_index()
        .rename(columns={"snapshot_date": "metric_date"})
    )
    inv["inventory_age_days"] = np.where(
        inv["on_hand_lb"] > 0, inv["age_lb"] / inv["on_hand_lb"], np.nan
    )
    out["daily_inventory_location"] = inv.drop(columns=["age_lb"])

    return out
