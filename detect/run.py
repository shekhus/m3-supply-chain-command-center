"""Run every detector over every watched grain — for a day, or for a range, which is what replay walks.

No LLM is imported anywhere under `detect/`. That is principle 1, and a test asserts it.

Work is organised per segment, not per day: each segment's series is built and its baseline computed once, and
then every day of the range reads those arrays. A replay of 500+ days over ~250 segments is one pass per
series instead of one baseline per detector per day.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import Engine

from detect import baseline as baseline_mod
from detect import cusum, severity, threshold
from detect.config import DetectConfig
from detect.models import Anomaly, Severity
from detect.series import GRAINS, Grain, SegmentSeries, load_tables, segments, series_for


def build_series(frames: dict[str, pd.DataFrame], cfg: DetectConfig,
                 grains: tuple[Grain, ...] = GRAINS) -> list[SegmentSeries]:
    """Every watched segment, with its baseline attached. Built once; a replay reuses it for every day."""
    built: list[SegmentSeries] = []
    for g in grains:
        tidy = series_for(frames, g)
        for segment in segments(tidy, g):
            series = SegmentSeries.build(tidy, g, segment, cfg.is_holiday)
            if len(series) < cfg.min_baseline_points:
                continue
            built.append(baseline_mod.enrich(series, g.metric, cfg))
        _set_volume_scale([s for s in built if s.grain is g])
    return built


def _set_volume_scale(grain_series: list[SegmentSeries]) -> None:
    """A typical segment's volume at this grain: the yardstick `Anomaly.rank` measures a segment against."""
    volumes = np.concatenate([s.volumes for s in grain_series]) if grain_series else np.array([1.0])
    scale = float(np.median(volumes[volumes > 0])) if (volumes > 0).any() else 1.0
    for s in grain_series:
        s.volume_scale = max(scale, 1e-9)


def detect_range(series: list[SegmentSeries], cfg: DetectConfig, from_date: date,
                 to_date: date) -> list[Anomaly]:
    found: list[Anomaly] = []
    for segment in series:
        for i, day in enumerate(segment.dates):
            if day < from_date or day > to_date:
                continue
            anomaly = _evaluate_day(segment, i, cfg)
            if anomaly is not None:
                found.append(anomaly)
    return sorted(found, key=lambda a: (a.metric_date, -a.severity, -a.rank))


def detect_day(series: list[SegmentSeries], cfg: DetectConfig, as_of: date) -> list[Anomaly]:
    return detect_range(series, cfg, as_of, as_of)


def _evaluate_day(series: SegmentSeries, i: int, cfg: DetectConfig) -> Anomaly | None:
    g, day = series.grain, series.dates[i]
    base = None
    if series.valid is not None and bool(series.valid[i]) and series.z is not None:
        assert series.expected is not None and series.std is not None and series.delta is not None
        base = baseline_mod.Baseline(
            expected=float(series.expected[i]), std=float(series.std[i]),
            points=int(series.points[i]) if series.points is not None else 0,
            value=float(series.values[i]), delta=float(series.delta[i]), z=float(series.z[i]),
            dow_offset=0.0, excluded_holidays=0)
    hit = threshold.evaluate(series, day, g.metric, cfg)
    shift = cusum.evaluate(series, day, cfg)
    return severity.combine(g, series.segment, day, float(series.values[i]), float(series.volumes[i]),
                            base, hit, shift, cfg, series.volume_scale)


def incidents(found: list[Anomaly]) -> list[Anomaly]:
    """What a human is asked to look at: WARN and above, strongest first. INFO stays in the record."""
    return sorted([a for a in found if a.severity >= Severity.WARN], key=lambda a: (-a.severity, -a.rank))


def load_frames(source: Engine | Path) -> dict[str, pd.DataFrame]:
    return load_tables(source)


def window_for(frames: dict[str, pd.DataFrame]) -> tuple[date, date]:
    """The days the metric tables actually cover, so a replay never claims a day that has no metrics."""
    firsts = [frame["metric_date"].min() for frame in frames.values()]
    lasts = [frame["metric_date"].max() for frame in frames.values()]
    return min(firsts), max(lasts)


def warmup_start(frames: dict[str, pd.DataFrame], cfg: DetectConfig) -> date:
    """The first day a detector may speak: before a full baseline exists, silence is the honest answer."""
    first, _ = window_for(frames)
    return first + timedelta(days=cfg.baseline_days)


def as_frame(found: list[Anomaly]) -> pd.DataFrame:
    """Anomalies as a table — what replay scores and what the ops page shows."""
    if not found:
        return pd.DataFrame(columns=["metric", "grain", "segment", "metric_date", "severity", "value",
                                     "expected", "delta", "volume", "magnitude", "rank"])
    rows = [{**a.to_dict(), "segment": "/".join(f"{k}={v}" for k, v in sorted(a.segment.items())) or "ALL"}
            for a in found]
    frame = pd.DataFrame(rows)
    frame["detectors"] = [",".join(s.detector for s in a.detectors) for a in found]
    return frame.replace({np.nan: None})
