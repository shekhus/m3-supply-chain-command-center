"""Hand-built series for detector tests: a known shape in, a known verdict out.

A detector tested only against the generated data is tested against one world. These series are small enough
to reason about on paper, which is the only way to know a z-score is right rather than merely stable.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date, timedelta

import pandas as pd

from detect.config import DetectConfig
from detect.series import Grain, SegmentSeries

GRAIN = Grain("test", "daily_otif_plant", ("plant",), "otif_rate", "lines", "down")
START = date(2026, 1, 5)   # a Monday, so weekday arithmetic in the tests is readable


def series(values: Sequence[float], cfg: DetectConfig, *, start: date = START, volume: float = 100.0,
           grain: Grain = GRAIN, skip: Sequence[date] = ()) -> SegmentSeries:
    """A daily series starting on `start`; `skip` drops days, so gap handling can be tested."""
    days = [start + timedelta(days=i) for i in range(len(values))]
    rows = [{"metric_date": d, "plant": "PLT-01", "value": float(v), "volume": volume}
            for d, v in zip(days, values, strict=True) if d not in set(skip)]
    tidy = pd.DataFrame(rows)
    return SegmentSeries.build(tidy, grain, {"plant": "PLT-01"}, cfg.is_holiday)


def enriched(values: Sequence[float], cfg: DetectConfig, metric: str = "otif_rate",
             **kwargs: object) -> SegmentSeries:
    from detect.baseline import enrich

    return enrich(series(values, cfg, **kwargs), metric, cfg)  # type: ignore[arg-type]


def weekly(pattern: Sequence[float], weeks: int, start: date = START) -> list[float]:
    """`pattern` is seven values, Monday first: a series with a real weekly shape and nothing else."""
    assert len(pattern) == 7
    offset = start.weekday()
    return [pattern[(offset + i) % 7] for i in range(weeks * 7)]


def config_with(cfg: DetectConfig, **overrides: object) -> DetectConfig:
    from dataclasses import replace

    return replace(cfg, **overrides)  # type: ignore[arg-type]


def flag_dates(found: Sequence, predicate: Callable) -> list[date]:
    return sorted({a.metric_date for a in found if predicate(a)})
