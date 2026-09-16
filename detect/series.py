"""Which series exist, at which grains, and where each one's numbers come from.

A detector is only as good as the grain it runs at. A lane collapse (A1) is invisible in the company total and
obvious at plant × customer; a weight-basis shortfall (A2) is invisible at plant level and obvious at plant ×
product group. So every metric declares the grains it is watched at, and the detectors run at all of them.

The whole table is read once and sliced in memory: a replay walks 500+ days, and a query per segment per day
would make the replay the slowest part of the project for no benefit.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import Engine


class SeriesError(RuntimeError):
    pass


@dataclass(frozen=True)
class Grain:
    """One watched series: a metric column in a metrics table, keyed by zero or more segment columns."""

    name: str
    table: str
    keys: tuple[str, ...]
    value: str
    volume: str            # the column that says how much this number is carrying
    direction: str         # the direction that is bad: "down", "up" or "both"

    @property
    def metric(self) -> str:
        return self.value


# Every metric policy.yaml sets a threshold for, at every grain an anomaly can live at.
GRAINS: tuple[Grain, ...] = (
    Grain("otif_total", "daily_otif_total", (), "otif_rate", "lines", "down"),
    Grain("otif_plant", "daily_otif_plant", ("plant",), "otif_rate", "lines", "down"),
    Grain("otif_lane", "daily_otif_lane", ("plant", "customer_no"), "otif_rate", "lines", "down"),
    Grain("fill_weight_total", "daily_otif_total", (), "fill_rate_weight", "lines", "down"),
    Grain("fill_weight_plant", "daily_otif_plant", ("plant",), "fill_rate_weight", "lines", "down"),
    Grain("fill_weight_group", "daily_fill_plant_group", ("plant", "product_group"),
          "fill_rate_weight", "lines", "down"),
    Grain("fill_count_group", "daily_fill_plant_group", ("plant", "product_group"),
          "fill_rate_count", "lines", "down"),
    Grain("yield_line", "daily_yield", ("plant", "line", "product_group"),
          "yield_variance_pct", "input_lb", "both"),
    Grain("inventory_age", "daily_inventory_location", ("location",), "inventory_age_days", "on_hand_lb",
          "up"),
    Grain("inventory_at_risk", "daily_inventory_location", ("location",),
          "lb_at_risk_within_5d", "on_hand_lb", "up"),
    Grain("backlog_plant", "daily_backlog_plant", ("plant",), "open_backlog_lines", "open_backlog_lines",
          "up"),
)

TABLES: tuple[str, ...] = tuple(dict.fromkeys(g.table for g in GRAINS))


def grains_for(metric: str) -> tuple[Grain, ...]:
    return tuple(g for g in GRAINS if g.value == metric)


def grain(name: str) -> Grain:
    for g in GRAINS:
        if g.name == name:
            return g
    raise SeriesError(f"unknown grain {name!r}")


def load_tables(source: Engine | Path) -> dict[str, pd.DataFrame]:
    """Read every metrics table once, from Postgres (production, replay) or from parquet (tests, no DB)."""
    frames: dict[str, pd.DataFrame] = {}
    for table in TABLES:
        if isinstance(source, Path):
            path = source / f"{table}.parquet"
            if not path.exists():
                raise SeriesError(f"{path} is missing; run `make synth-gold`")
            frame = pd.read_parquet(path)
        else:
            frame = pd.read_sql(f"SELECT * FROM metrics.{table}", source)
        if frame.empty:
            raise SeriesError(f"metrics.{table} is empty; run `make metrics`")
        frame["metric_date"] = pd.to_datetime(frame["metric_date"]).dt.date
        frames[table] = frame.sort_values("metric_date").reset_index(drop=True)
    return frames


def series_for(frames: dict[str, pd.DataFrame], g: Grain) -> pd.DataFrame:
    """One tidy frame per grain: metric_date, the segment keys, `value`, `volume`. Valueless rows are dropped.

    A missing day is left missing rather than filled with a zero: an absent number is not a bad number, and
    the baseline must not be dragged down by days the business did not trade.
    """
    frame = frames[g.table]
    for column in (*g.keys, g.value, g.volume):
        if column not in frame.columns:
            raise SeriesError(f"metrics.{g.table} has no column {column!r} (grain {g.name})")
    columns = list(dict.fromkeys(["metric_date", *g.keys, g.value, g.volume]))
    tidy = frame[columns].copy()
    if g.value == g.volume:  # backlog: the count is both the level and the weight behind it
        tidy = tidy.rename(columns={g.value: "value"}).assign(volume=lambda f: f["value"])
    else:
        tidy = tidy.rename(columns={g.value: "value", g.volume: "volume"})
    tidy = tidy.dropna(subset=["value"])
    tidy["volume"] = tidy["volume"].fillna(0.0).astype(float)
    tidy["value"] = tidy["value"].astype(float)
    return tidy.sort_values(["metric_date", *g.keys]).reset_index(drop=True)


def segments(tidy: pd.DataFrame, g: Grain) -> list[dict[str, str]]:
    if not g.keys:
        return [{}]
    unique = tidy[list(g.keys)].drop_duplicates().sort_values(list(g.keys))
    return [{k: str(v) for k, v in row.items()} for row in unique.to_dict("records")]


def segment_series(tidy: pd.DataFrame, g: Grain, segment: dict[str, str]) -> pd.DataFrame:
    if not g.keys:
        return tidy
    mask = pd.Series(True, index=tidy.index)
    for key in g.keys:
        mask &= tidy[key].astype(str) == segment[key]
    return tidy[mask]


@dataclass
class SegmentSeries:
    """One segment's history as plain arrays, with room for the baseline columns the detectors share.

    A replay evaluates every day of every segment, and each day looks back four weeks. Held as a DataFrame
    that is 500 boolean-mask filters per segment; held as sorted arrays it is a slice. The detectors read
    this, so the baseline is computed once per day and not once per detector per day.
    """

    grain: Grain
    segment: dict[str, str]
    dates: list[date]
    ordinals: np.ndarray
    values: np.ndarray
    volumes: np.ndarray
    weekday: np.ndarray
    holiday: np.ndarray
    index: dict[date, int] = field(default_factory=dict)
    volume_scale: float = 1.0   # a typical segment's volume at this grain (set once, in build_series)
    expected: np.ndarray | None = None
    std: np.ndarray | None = None
    delta: np.ndarray | None = None
    z: np.ndarray | None = None
    points: np.ndarray | None = None
    valid: np.ndarray | None = None

    def __len__(self) -> int:
        return len(self.dates)

    @classmethod
    def build(cls, tidy: pd.DataFrame, g: Grain, segment: dict[str, str],
              is_holiday: Callable[[date], bool]) -> SegmentSeries:
        rows = segment_series(tidy, g, segment).sort_values("metric_date")
        dates = [d for d in rows["metric_date"]]
        return cls(
            grain=g, segment=segment, dates=dates,
            ordinals=np.array([d.toordinal() for d in dates], dtype=np.int64),
            values=rows["value"].to_numpy(dtype=float),
            volumes=rows["volume"].to_numpy(dtype=float),
            weekday=np.array([d.weekday() for d in dates], dtype=np.int64),
            holiday=np.array([is_holiday(d) for d in dates], dtype=bool),
            index={d: i for i, d in enumerate(dates)})

    def window(self, i: int, days: int) -> slice:
        """The `days` calendar days before day `i` — by date, not by position: missing days stay missing."""
        lo = int(np.searchsorted(self.ordinals, self.ordinals[i] - days, side="left"))
        return slice(lo, i)
