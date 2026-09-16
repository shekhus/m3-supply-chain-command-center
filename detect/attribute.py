"""Why did it move? Decomposition in code, so the brief's "71%" is a number and not a turn of phrase.

A rate at a coarse grain is a weighted average of the same rate at a finer one, and the movement between two
windows splits exactly into one contribution per finer segment:

    R₁ − R₀ = Σᵢ [ wᵢ₁(rᵢ₁ − rᵢ₀) ]   ← the rate effect: this segment got worse
            + Σᵢ [ rᵢ₀(wᵢ₁ − wᵢ₀) ]   ← the mix effect: this segment is a bigger share of the volume

where rᵢ is segment i's own rate and wᵢ its share of the denominator. The identity is exact, so the
contributions sum to the movement being explained — which is the property that makes the percentage
publishable. Both halves are reported, and **drivers are ranked by the rate effect**, because "the lane
collapsed" and "we shipped more through the lane that was always worst" are different problems with different
owners. Ranking on the sum answers neither: a short window's volume mix wanders against a 28-day baseline, and
those mix terms are large enough to bury the segment that actually got worse. When the mix half dominates the
movement overall, the attribution says so rather than naming a culprit who did nothing differently.

Additive metrics (open backlog lines, pounds at risk) need no weighting: the parent is the sum of its parts,
so a contribution is just the part's own movement.

Nothing here is an estimate, and nothing here is a model: the same inputs give the same decomposition every
time, and every driver carries the evidence refs for the rows it was computed from.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, timedelta

import numpy as np
import pandas as pd

from detect.config import DetectConfig
from detect.models import Anomaly, EvidenceRef
from detect.series import GRAINS, Grain, SeriesError, segments, series_for

# Which finer grain explains a movement at this one. A grain absent here is already as fine as the data goes.
CHILDREN: dict[str, str] = {
    "otif_total": "otif_plant",
    "otif_plant": "otif_lane",
    "fill_weight_total": "fill_weight_plant",
    "fill_weight_plant": "fill_weight_group",
}

# What each metric is an average *of*. A metric absent here is additive: the parent is the sum of its parts.
DENOMINATOR: dict[str, str] = {
    "otif_rate": "lines",
    "on_time_rate": "lines",
    "fill_rate_count": "lines",
    "fill_rate_weight": "ordered_weight_lb",
    "yield_variance_pct": "input_lb",
    "inventory_age_days": "on_hand_lb",
}


@dataclass(frozen=True)
class Driver:
    """One finer segment's share of the movement. `contribution` is in the metric's own units."""

    segment: dict[str, str]
    contribution: float
    contribution_pct: float      # share of the whole movement, rate and mix together
    rate_effect_pct: float       # share of the performance change alone — the number a brief should quote
    rate_effect: float
    mix_effect: float
    window_value: float
    baseline_value: float
    window_volume: float
    baseline_volume: float
    evidence_refs: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return "/".join(f"{k}={v}" for k, v in sorted(self.segment.items())) or "ALL"


@dataclass
class Attribution:
    anomaly_key: str
    grain: str                 # the grain the drivers are at ("" when nothing finer exists)
    window: tuple[date, date]
    baseline: tuple[date, date]
    total_delta: float
    drivers: list[Driver] = field(default_factory=list)
    note: str | None = None
    rate_effect_total: float = 0.0
    mix_effect_total: float = 0.0

    @property
    def dominant_effect(self) -> str:
        """Whether this movement is performance ("rate") or a change in what was shipped ("mix")."""
        return "mix" if abs(self.mix_effect_total) > abs(self.rate_effect_total) else "rate"

    @property
    def explained_pct(self) -> float:
        """How much of the movement the listed drivers account for. The identity is exact over *all* segments,
        so a top-3 list explaining 40% is a fact about the anomaly — it was broad — and one explaining 150%
        means the rest of the segments moved the other way. Neither is an error in the arithmetic."""
        if self.total_delta == 0:
            return 0.0
        return sum(d.contribution for d in self.drivers) / self.total_delta * 100.0

    def to_dict(self) -> dict:
        return {
            "anomaly": self.anomaly_key,
            "grain": self.grain,
            "window": [self.window[0].isoformat(), self.window[1].isoformat()],
            "baseline": [self.baseline[0].isoformat(), self.baseline[1].isoformat()],
            "total_delta": self.total_delta,
            "explained_pct": self.explained_pct,
            "rate_effect_total": self.rate_effect_total,
            "mix_effect_total": self.mix_effect_total,
            "dominant_effect": self.dominant_effect,
            "note": self.note,
            "drivers": [{"segment": d.segment, "label": d.label, "contribution": d.contribution,
                         "contribution_pct": d.contribution_pct, "rate_effect_pct": d.rate_effect_pct,
                         "rate_effect": d.rate_effect,
                         "mix_effect": d.mix_effect, "window_value": d.window_value,
                         "baseline_value": d.baseline_value, "window_volume": d.window_volume,
                         "evidence_refs": d.evidence_refs} for d in self.drivers],
        }


def child_grain(parent: Grain) -> Grain | None:
    name = CHILDREN.get(parent.name)
    if name is None:
        return None
    child = next((g for g in GRAINS if g.name == name), None)
    if child is None:
        raise SeriesError(f"{parent.name} names a child grain {name!r} that does not exist")
    if not set(parent.keys) <= set(child.keys):
        raise SeriesError(f"{child.name} is not finer than {parent.name}")
    return child


def attribute(frames: dict[str, pd.DataFrame], cfg: DetectConfig, anomaly: Anomaly,
              top: int = 3) -> Attribution:
    """Decompose one anomaly's movement into the finer segments underneath it, strongest effect first."""
    parent = next(g for g in GRAINS if g.name == anomaly.grain)
    window = (anomaly.window_start, anomaly.metric_date)
    baseline = (anomaly.window_start - timedelta(days=cfg.baseline_days),
                anomaly.window_start - timedelta(days=1))
    key = f"{anomaly.metric}:{'/'.join(f'{k}={v}' for k, v in sorted(anomaly.segment.items())) or 'ALL'}"

    child = child_grain(parent)
    if child is None:
        return Attribution(anomaly_key=key, grain="", window=window, baseline=baseline,
                           total_delta=anomaly.delta or 0.0,
                           note=f"{parent.name} is the finest grain held for {anomaly.metric}")

    tidy = series_for(frames, child)
    for k, v in anomaly.segment.items():   # stay inside the parent: a plant's drop is its own lanes' doing
        tidy = tidy[tidy[k].astype(str) == v]
    if tidy.empty:
        return Attribution(anomaly_key=key, grain=child.name, window=window, baseline=baseline,
                           total_delta=anomaly.delta or 0.0, note="no rows at the finer grain")

    denominator = DENOMINATOR.get(anomaly.metric)
    drivers, total = (_weighted(tidy, child, window, baseline, cfg) if denominator
                      else _additive(tidy, child, window, baseline, cfg))
    drivers.sort(key=lambda d: -abs(d.rate_effect))
    rate_total = sum(d.rate_effect for d in drivers)
    mix_total = sum(d.mix_effect for d in drivers)
    # "this lane is 71% of the drop" means 71% of the performance change: a share of the whole movement can
    # exceed 100% or flip sign when the mix moved the other way, which is true and unpublishable.
    drivers = [replace(d, rate_effect_pct=d.rate_effect / rate_total * 100.0 if rate_total else 0.0)
               for d in drivers]
    note = None if drivers else "no finer segment moved"
    if drivers and abs(mix_total) > abs(rate_total):
        note = ("the movement is mostly a change in what was shipped, not in how any segment performed "
                f"(mix {mix_total:+.4g} against rate {rate_total:+.4g})")
    return Attribution(anomaly_key=key, grain=child.name, window=window, baseline=baseline,
                       total_delta=total, drivers=drivers[:top], note=note,
                       rate_effect_total=rate_total, mix_effect_total=mix_total)


def _slice(tidy: pd.DataFrame, span: tuple[date, date], cfg: DetectConfig) -> pd.DataFrame:
    """Rows inside a span, holidays dropped: the same days the baseline detector was allowed to learn from."""
    rows = tidy[(tidy["metric_date"] >= span[0]) & (tidy["metric_date"] <= span[1])]
    return rows[~rows["metric_date"].map(cfg.is_holiday)]


def _totals(rows: pd.DataFrame, child: Grain) -> pd.DataFrame:
    """Per segment: the volume-weighted value and the volume behind it, over the whole span."""
    keys = list(child.keys)
    frame = rows.assign(_num=rows["value"] * rows["volume"])
    grouped = frame.groupby(keys, dropna=False).agg(num=("_num", "sum"), den=("volume", "sum")).reset_index()
    grouped["value"] = np.where(grouped["den"] > 0, grouped["num"] / grouped["den"], np.nan)
    return grouped


def _weighted(tidy: pd.DataFrame, child: Grain, window: tuple[date, date], baseline: tuple[date, date],
              cfg: DetectConfig) -> tuple[list[Driver], float]:
    """The exact rate/mix decomposition. Contributions sum to the parent's movement, by construction."""
    window_rows = _slice(tidy, window, cfg)
    used = _days_used(window_rows)
    now, before = _totals(window_rows, child), _totals(_slice(tidy, baseline, cfg), child)
    keys = list(child.keys)
    joined = now.merge(before, on=keys, how="outer", suffixes=("_1", "_0")).fillna(
        {"num_1": 0.0, "den_1": 0.0, "num_0": 0.0, "den_0": 0.0})

    den_1, den_0 = float(joined["den_1"].sum()), float(joined["den_0"].sum())
    if den_1 <= 0 or den_0 <= 0:
        return [], 0.0
    parent_1, parent_0 = float(joined["num_1"].sum()) / den_1, float(joined["num_0"].sum()) / den_0
    total = parent_1 - parent_0

    drivers = []
    for row in joined.to_dict("records"):
        w_1, w_0 = row["den_1"] / den_1, row["den_0"] / den_0
        # a segment absent from one window is given the parent's rate there: it did not move on its own
        r_1 = row["num_1"] / row["den_1"] if row["den_1"] > 0 else parent_0
        r_0 = row["num_0"] / row["den_0"] if row["den_0"] > 0 else parent_0
        rate_effect = w_1 * (r_1 - r_0)
        mix_effect = r_0 * (w_1 - w_0)
        contribution = rate_effect + mix_effect
        if contribution == 0:
            continue
        segment = {k: str(row[k]) for k in keys}
        drivers.append(Driver(
            segment=segment, contribution=contribution,
            contribution_pct=contribution / total * 100.0 if total else 0.0, rate_effect_pct=0.0,
            rate_effect=rate_effect, mix_effect=mix_effect, window_value=r_1, baseline_value=r_0,
            window_volume=row["den_1"], baseline_volume=row["den_0"],
            evidence_refs=_refs(child, segment, used)))
    return drivers, total


def _additive(tidy: pd.DataFrame, child: Grain, window: tuple[date, date], baseline: tuple[date, date],
              cfg: DetectConfig) -> tuple[list[Driver], float]:
    """Counts and pounds: the parent is the sum of its parts, so a part's contribution is its own movement."""
    keys = list(child.keys)
    window_rows = _slice(tidy, window, cfg)
    used = _days_used(window_rows)
    now = window_rows.groupby(keys, dropna=False)["value"].mean()
    before = _slice(tidy, baseline, cfg).groupby(keys, dropna=False)["value"].mean()
    joined = pd.concat([now.rename("v1"), before.rename("v0")], axis=1).fillna(0.0).reset_index()
    total = float(joined["v1"].sum() - joined["v0"].sum())

    drivers = []
    for row in joined.to_dict("records"):
        contribution = float(row["v1"] - row["v0"])
        if contribution == 0:
            continue
        segment = {k: str(row[k]) for k in keys}
        drivers.append(Driver(
            segment=segment, contribution=contribution,
            contribution_pct=contribution / total * 100.0 if total else 0.0, rate_effect_pct=0.0,
            rate_effect=contribution, mix_effect=0.0, window_value=float(row["v1"]),
            baseline_value=float(row["v0"]), window_volume=float(row["v1"]),
            baseline_volume=float(row["v0"]), evidence_refs=_refs(child, segment, used)))
    return drivers, total


def _days_used(window_rows: pd.DataFrame) -> list[date]:
    """The first and last day that actually entered the arithmetic — holidays are not among them."""
    if window_rows.empty:
        return []
    days = sorted(set(window_rows["metric_date"]))
    return list(dict.fromkeys([days[0], days[-1]]))


def _refs(child: Grain, segment: dict[str, str], days: list[date]) -> list[str]:
    """Enough for a reader to find the rows without listing all 21, and never a row that was excluded."""
    return [EvidenceRef(child.table, segment, day, child.value).ref for day in days]


def attribute_all(frames: dict[str, pd.DataFrame], cfg: DetectConfig, anomalies: list[Anomaly],
                  top: int = 3) -> dict[str, Attribution]:
    """Attribution for a day's incidents, keyed by the anomaly's own key — what the evidence pack carries."""
    out: dict[str, Attribution] = {}
    for anomaly in anomalies:
        result = attribute(frames, cfg, anomaly, top)
        out[result.anomaly_key] = result
    return out


def segment_labels(frames: dict[str, pd.DataFrame], grain_name: str) -> list[str]:
    """Every segment at a grain — used by tests and the console to show what could have been named."""
    g = next(g for g in GRAINS if g.name == grain_name)
    tidy = series_for(frames, g)
    return ["/".join(f"{k}={v}" for k, v in sorted(s.items())) or "ALL" for s in segments(tidy, g)]
