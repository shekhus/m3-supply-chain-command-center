"""Prove the seeded anomalies are really in the data, then write the answer key.

Lifted from the generator in m3-trusted-data-foundation (its `synth/truth.py`), keeping only the anomaly
parts: this repository generates the same world from the same seed, so the two answer keys agree by
construction. The self-check recomputes each seeded anomaly at the grain it was planted at, compares its
window against a baseline period, and fails generation when an effect is absent or points the wrong way — a
dataset whose ground truth is a lie would silently invalidate every measurement downstream.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

import pandas as pd

from . import config as C


def _window_vs_baseline(
    df: pd.DataFrame, date_col: str, value_col: str, a: C.Anomaly,
    weight_col: str | None = None, baseline_days: int = 56,
) -> dict:
    d = df.copy()
    d[date_col] = pd.to_datetime(d[date_col]).dt.date

    win = d[(d[date_col] >= a.start) & (d[date_col] <= a.end)]
    base = d[
        (d[date_col] >= a.start - timedelta(days=baseline_days))
        & (d[date_col] < a.start)
    ]
    if win.empty or base.empty:
        return {"error": "no rows in window or baseline", "window_rows": len(win),
                "baseline_rows": len(base)}

    def agg(x: pd.DataFrame) -> float:
        if weight_col:
            w = x[weight_col].sum()
            return float((x[value_col] * x[weight_col]).sum() / w) if w else float("nan")
        return float(x[value_col].mean())

    wv, bv = agg(win), agg(base)
    return {
        "window_value": round(wv, 5),
        "baseline_value": round(bv, 5),
        "delta": round(wv - bv, 5),
        "window_rows": int(len(win)),
        "baseline_rows": int(len(base)),
    }


def self_check(metrics: dict[str, pd.DataFrame]) -> tuple[dict[str, dict], list[str]]:
    """
    Verify every seeded anomaly is measurably present. Returns (evidence, failures).
    """
    evidence: dict[str, dict] = {}
    failures: list[str] = []

    def check(aid: str, res: dict, direction: str, min_abs: float) -> None:
        evidence[aid] = res
        if "error" in res:
            failures.append(f"{aid}: {res['error']}")
            return
        delta = res["delta"]
        ok_dir = delta < 0 if direction == "down" else delta > 0
        if not (ok_dir and abs(delta) >= min_abs):
            failures.append(
                f"{aid}: expected {direction} shift of at least {min_abs}, got {delta}"
            )

    a = {x.anomaly_id: x for x in C.ANOMALIES}

    # A1 — OTIF on the PLT-02 > C000031 lane should drop materially
    lane = metrics["daily_otif_lane"]
    lane_sel = lane[
        (lane["plant"] == a["A1"].segment["plant"])
        & (lane["customer_no"] == a["A1"].segment["customer_no"])
    ]
    check("A1", _window_vs_baseline(lane_sel, "metric_date", "otif_rate", a["A1"],
                                    weight_col="lines"), "down", 0.15)

    # A2 — weight fill rate for CASE-READY at PLT-01 should drop
    fpg = metrics["daily_fill_plant_group"]
    fpg_sel = fpg[
        (fpg["plant"] == a["A2"].segment["plant"])
        & (fpg["product_group"] == a["A2"].segment["product_group"])
    ]
    check("A2", _window_vs_baseline(fpg_sel, "metric_date", "fill_rate_weight", a["A2"],
                                    weight_col="ordered_weight_lb"), "down", 0.04)

    # A3 — yield variance on PLT-03 L2 PRIMALS should go sharply negative
    dy = metrics["daily_yield"]
    dy_sel = dy[
        (dy["plant"] == a["A3"].segment["plant"])
        & (dy["line"] == a["A3"].segment["line"])
        & (dy["product_group"] == a["A3"].segment["product_group"])
    ]
    check("A3", _window_vs_baseline(dy_sel, "metric_date", "yield_variance_pct", a["A3"]),
          "down", 3.0)

    # A4 — mean inventory age at DC-EAST should rise
    inv = metrics["daily_inventory_location"]
    inv_sel = inv[inv["location"] == a["A4"].segment["location"]]
    check("A4", _window_vs_baseline(inv_sel, "metric_date", "inventory_age_days", a["A4"]),
          "up", 1.0)

    # A5 — the decoy must be REAL in the data (a genuine dip) so that the
    # detector's seasonality awareness is what distinguishes it, not its absence
    tot = metrics["daily_otif_total"]
    check("A5", _window_vs_baseline(tot, "metric_date", "otif_rate", a["A5"],
                                    weight_col="lines", baseline_days=28), "down", 0.05)

    # A6 — open backlog at PLT-02 should rise
    bl = metrics["daily_backlog_plant"]
    bl_sel = bl[bl["plant"] == a["A6"].segment["plant"]]
    check("A6", _window_vs_baseline(bl_sel, "metric_date", "open_backlog_lines", a["A6"]),
          "up", 20.0)

    return evidence, failures


def write_anomalies(out: Path, evidence: dict[str, dict]) -> None:
    payload = {
        "scoring": {
            "recall": "seeded anomalies with expect_detection=true that the "
                      "detector flags at WARN or above, within the window",
            "precision": "flags outside any seeded window count as false "
                         "positives; A5 flagged at HIGH counts as a false positive",
            "lead_time": "days from anomaly start to first flag",
        },
        "anomalies": [
            {**asdict(a), "start": str(a.start), "end": str(a.end),
             "observed": evidence.get(a.anomaly_id, {})}
            for a in C.ANOMALIES
        ],
    }
    (out / "anomalies.json").write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8", newline="\n"
    )
