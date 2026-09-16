"""
Generate the operational event streams: order lines with deliveries, daily
inventory snapshots, and production yield runs.

Seeded anomalies are injected HERE, at the level of the underlying cause (late
issue dates, short weights, depressed output, stalled lots, order surges), not
by editing a metric afterwards. That matters: it means the anomalies are
discoverable by any correct metric implementation, and the attribution logic has
a real signal to find.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from . import config as C

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def business_days(start: date, end: date) -> list[date]:
    """Mon-Sat shipping week; Sunday is dark."""
    days, d = [], start
    while d <= end:
        if d.weekday() != 6:
            days.append(d)
        d += timedelta(days=1)
    return days


def _in_window(d: date, a: C.Anomaly) -> bool:
    return a.start <= d <= a.end


def _anomaly(anomaly_id: str) -> C.Anomaly:
    return next(a for a in C.ANOMALIES if a.anomaly_id == anomaly_id)


# ---------------------------------------------------------------------------
# order lines + deliveries
# ---------------------------------------------------------------------------

def generate_order_lines(
    rng: np.random.Generator,
    customers: pd.DataFrame,
    items: pd.DataFrame,
) -> pd.DataFrame:
    """
    One row per order line, already joined with its delivery outcome.

    Columns are CANONICAL. Source-specific mangling happens later in export.py.
    """
    a1, a2, a5, a6 = _anomaly("A1"), _anomaly("A2"), _anomaly("A5"), _anomaly("A6")

    cust_no = customers["customer_no"].to_numpy()
    cust_p = customers["volume_share"].to_numpy()
    item_no = items["item_no"].to_numpy()
    item_p = items["volume_share"].to_numpy()

    item_lookup = items.set_index("item_no")[
        ["product_group", "catch_weight_flag", "avg_lb_per_case", "shelf_life_days"]
    ].to_dict("index")

    plant_codes = np.array([p.code for p in C.PLANTS])
    plant_p = np.array([p.share for p in C.PLANTS])
    plant_p = plant_p / plant_p.sum()

    days = business_days(C.START_DATE, C.END_DATE)
    rows: list[dict] = []
    order_seq = 0

    for d in days:
        n_lines = int(max(20, rng.normal(C.LINES_PER_DAY_MEAN, C.LINES_PER_DAY_SD)))

        # A6: order surge at PLT-02 inflates that plant's volume.
        surge_extra = 0
        if _in_window(d, a6):
            surge_extra = int(n_lines * (a6.magnitude - 1.0) * C.PLANTS[1].share)

        line_plants = list(rng.choice(plant_codes, size=n_lines, p=plant_p))
        line_plants += ["PLT-02"] * surge_extra

        # Group lines into orders of 1-5 lines.
        i = 0
        while i < len(line_plants):
            order_lines = min(len(line_plants) - i, int(rng.integers(1, 6)))
            order_seq += 1
            order_no = f"SO{order_seq:07d}"
            plant = line_plants[i]

            customer = str(rng.choice(cust_no, p=cust_p))
            # A1's lane needs enough traffic for attribution to be meaningful.
            if plant == "PLT-02" and rng.random() < 0.08:
                customer = "C000031"

            order_date = d
            lead_days = int(rng.integers(3, 15))
            requested = order_date + timedelta(days=lead_days)
            # Confirmed date usually equals requested; sometimes the plant pushes it.
            confirmed = requested + (
                timedelta(days=int(rng.integers(1, 4))) if rng.random() < 0.18 else timedelta(0)
            )

            for ln in range(1, order_lines + 1):
                item = str(rng.choice(item_no, p=item_p))
                meta = item_lookup[item]
                group = meta["product_group"]
                catch_weight = bool(meta["catch_weight_flag"])
                lb_per_case = float(meta["avg_lb_per_case"])

                ordered_qty = float(int(max(1, rng.gamma(shape=2.5, scale=14))))
                ordered_weight = round(ordered_qty * lb_per_case, 2)

                # --- on-time behaviour -------------------------------------
                p_on_time = (
                    C.BASE_ON_TIME_RATE
                    + C.PLANT_ON_TIME_DELTA[plant]
                    + C.DOW_ON_TIME_DELTA[confirmed.weekday()]
                )
                if confirmed in C.KNOWN_HOLIDAYS:
                    p_on_time -= 0.30

                late_days = 0
                # A1: late issue dates on the PLT-02 -> C000031 lane.
                a1_hit = (
                    _in_window(confirmed, a1)
                    and plant == a1.segment["plant"]
                    and customer == a1.segment["customer_no"]
                )
                if a1_hit and rng.random() < a1.magnitude:
                    late_days = int(rng.integers(1, 7))
                elif a1_hit:
                    late_days = 0
                # A5: holiday shutdown pushes a share of all lines late (decoy).
                elif _in_window(confirmed, a5) and rng.random() < a5.magnitude:
                    late_days = int(rng.integers(1, 4))
                elif rng.random() > np.clip(p_on_time, 0.5, 0.995):
                    late_days = int(rng.integers(1, 6))

                issue_date = confirmed + timedelta(days=late_days)

                # --- fill behaviour ----------------------------------------
                short_case = rng.random() < C.BASE_SHORT_CASE_RATE
                invoiced_qty = (
                    float(int(ordered_qty * rng.uniform(0.80, 0.98))) if short_case else ordered_qty
                )
                invoiced_qty = max(0.0, invoiced_qty)

                if catch_weight:
                    wt_ratio = float(rng.normal(C.CATCH_WEIGHT_MEAN, C.CATCH_WEIGHT_SD))
                else:
                    wt_ratio = 1.0

                # A2: short catch weights, case-ready at PLT-01.
                a2_hit = (
                    _in_window(issue_date, a2)
                    and plant == a2.segment["plant"]
                    and group == a2.segment["product_group"]
                )
                if a2_hit:
                    wt_ratio = float(rng.normal(a2.magnitude, 0.02))

                invoiced_weight = round(
                    ordered_weight * (invoiced_qty / ordered_qty) * wt_ratio, 2
                ) if ordered_qty else 0.0

                shelf_life = int(meta["shelf_life_days"])
                production_date = issue_date - timedelta(days=int(rng.integers(1, 5)))
                lot_no = f"L{plant[-2:]}{production_date.strftime('%y%m%d')}{rng.integers(100, 999)}"

                rows.append(
                    {
                        "order_no": order_no,
                        "line_no": ln,
                        "customer_no": customer,
                        "item_no": item,
                        "plant": plant,
                        "product_group": group,
                        "catch_weight_flag": catch_weight,
                        "order_date": order_date,
                        "requested_date": requested,
                        "confirmed_delivery_date": confirmed,
                        "issue_date": issue_date,
                        "ordered_qty": ordered_qty,
                        "invoiced_qty": invoiced_qty,
                        "ordered_weight_lb": ordered_weight,
                        "invoiced_weight_lb": invoiced_weight,
                        "uom": "CASE",
                        "lot_no": lot_no,
                        "production_date": production_date,
                        "expiry_date": production_date + timedelta(days=shelf_life),
                        "customer_po": f"PO{rng.integers(100000, 999999)}",
                        "order_type": str(rng.choice(["STD", "STD", "STD", "RUSH", "CONS"])),
                        # provenance for the anomaly evals
                        "_anomaly_id": "A1" if a1_hit else ("A2" if a2_hit else
                                       ("A5" if late_days and _in_window(confirmed, a5) else "")),
                    }
                )
            i += order_lines

    df = pd.DataFrame(rows)
    return df


# ---------------------------------------------------------------------------
# inventory snapshots
# ---------------------------------------------------------------------------

def generate_inventory(
    rng: np.random.Generator,
    items: pd.DataFrame,
) -> pd.DataFrame:
    """
    Daily on-hand by location x item x lot. Lots are created, drawn down over a
    few days, and retired. A4 stalls two lots at DC-EAST so their age climbs.
    """
    a4 = _anomaly("A4")
    locations = [w[0] for w in C.WAREHOUSES]
    item_meta = items.set_index("item_no")[["shelf_life_days", "avg_lb_per_case"]].to_dict("index")
    item_pool = items["item_no"].tolist()

    days = business_days(C.START_DATE, C.END_DATE)
    open_lots: list[dict] = []
    rows: list[dict] = []
    lot_seq = 0
    stalled_created = False

    for d in days:
        # retire exhausted / expired lots
        open_lots = [lot for lot in open_lots if lot["on_hand_lb"] > 50 and lot["expiry_date"] >= d]

        # create new lots
        for _ in range(int(rng.integers(3, 8))):
            lot_seq += 1
            item = str(rng.choice(item_pool))
            shelf_life = int(item_meta[item]["shelf_life_days"])
            loc = str(rng.choice(locations))
            open_lots.append(
                {
                    "lot_no": f"IL{lot_seq:07d}",
                    "location": loc,
                    "item_no": item,
                    "production_date": d - timedelta(days=int(rng.integers(0, 3))),
                    "expiry_date": d + timedelta(days=shelf_life),
                    "on_hand_lb": float(round(rng.gamma(shape=3.0, scale=1400), 1)),
                    "stalled": False,
                }
            )

        # A4: stall two DC-EAST lots for the window
        if _in_window(d, a4) and not stalled_created:
            for k in range(int(a4.magnitude)):
                lot_seq += 1
                item = str(rng.choice(item_pool))
                shelf_life = int(item_meta[item]["shelf_life_days"])
                open_lots.append(
                    {
                        "lot_no": f"IL{lot_seq:07d}",
                        "location": a4.segment["location"],
                        "item_no": item,
                        "production_date": d - timedelta(days=2),
                        # dated so the lot both ages and approaches expiry
                        # across the window — this is what makes it a risk
                        "expiry_date": d + timedelta(days=32),
                        "on_hand_lb": 9500.0 + 1500.0 * k,
                        "stalled": True,
                    }
                )
            stalled_created = True
        if d > a4.end:
            open_lots = [lot for lot in open_lots if not lot["stalled"]]

        # draw down and snapshot
        for lot in open_lots:
            if not lot["stalled"]:
                lot["on_hand_lb"] = float(round(lot["on_hand_lb"] * rng.uniform(0.55, 0.85), 1))
            rows.append(
                {
                    "snapshot_date": d,
                    "location": lot["location"],
                    "item_no": lot["item_no"],
                    "lot_no": lot["lot_no"],
                    "on_hand_lb": lot["on_hand_lb"],
                    "production_date": lot["production_date"],
                    "expiry_date": lot["expiry_date"],
                    "_anomaly_id": "A4" if lot["stalled"] else "",
                }
            )

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# production yield
# ---------------------------------------------------------------------------

def generate_yield(rng: np.random.Generator, items: pd.DataFrame) -> pd.DataFrame:
    """One row per plant x line x product_group x production day."""
    a3 = _anomaly("A3")
    std_by_group = {g: v[1] for g, v in C.PRODUCT_GROUPS.items()}
    groups = list(C.PRODUCT_GROUPS.keys())

    rows = []
    for d in business_days(C.START_DATE, C.END_DATE):
        if d.weekday() == 5 and rng.random() < 0.5:
            continue  # partial Saturday running
        for plant in C.PLANTS:
            for line in plant.lines:
                for group in rng.choice(groups, size=int(rng.integers(1, 4)), replace=False):
                    std = std_by_group[str(group)]
                    input_lb = float(round(rng.gamma(shape=6.0, scale=3200), 1))
                    actual = std + float(rng.normal(0, C.YIELD_NOISE_SD))

                    hit = (
                        _in_window(d, a3)
                        and plant.code == a3.segment["plant"]
                        and line == a3.segment["line"]
                        and str(group) == a3.segment["product_group"]
                    )
                    if hit:
                        actual = std * (1.0 - a3.magnitude) + float(rng.normal(0, 0.006))

                    rows.append(
                        {
                            "run_date": d,
                            "plant": plant.code,
                            "line": line,
                            "product_group": str(group),
                            "input_lb": input_lb,
                            "output_lb": float(round(input_lb * max(0.3, actual), 1)),
                            "std_yield_pct": float(round(std, 4)),
                            "_anomaly_id": "A3" if hit else "",
                        }
                    )

    # A3 needs the affected combination to run every day of its window,
    # otherwise the anomaly is intermittent and unfairly hard to score.
    existing = {
        (r.run_date, r.plant, r.line, r.product_group)
        for r in pd.DataFrame(rows).itertuples()
    }
    d = a3.start
    while d <= a3.end:
        key = (d, a3.segment["plant"], a3.segment["line"], a3.segment["product_group"])
        if d.weekday() != 6 and key not in existing:
            std = std_by_group[a3.segment["product_group"]]
            input_lb = float(round(rng.gamma(shape=6.0, scale=3200), 1))
            actual = std * (1.0 - a3.magnitude) + float(rng.normal(0, 0.006))
            rows.append(
                {
                    "run_date": d, "plant": a3.segment["plant"], "line": a3.segment["line"],
                    "product_group": a3.segment["product_group"],
                    "input_lb": input_lb,
                    "output_lb": float(round(input_lb * max(0.3, actual), 1)),
                    "std_yield_pct": float(round(std, 4)),
                    "_anomaly_id": "A3",
                }
            )
        d += timedelta(days=1)

    return pd.DataFrame(rows).sort_values(["run_date", "plant", "line"]).reset_index(drop=True)
