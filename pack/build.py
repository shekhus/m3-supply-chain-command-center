"""Build one morning's evidence pack: detect, attribute, rank, cap, round, name every number.

Everything a brief could say is decided here, in code, before a model is involved. In particular the rounding:
a rate is turned into "74.1%" exactly once, and the narration is asked to copy that string. A model given
0.74138 and asked to write it as a percentage will sometimes write 74%, sometimes 74.14%, and occasionally
74.8% — and the last one is indistinguishable from the others in a paragraph of confident prose.

What is deliberately *not* in the pack: the metrics tables, the history beyond a short context series, the
rankings' arithmetic, and any segment the audience may not see. A model cannot quote what it was never
given.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Literal

import pandas as pd

from detect.attribute import Attribution, attribute
from detect.config import DetectConfig
from detect.models import Anomaly, Severity
from detect.run import build_series, detect_range, incidents
from detect.series import SegmentSeries
from pack.permissions import DIRECTORY, Audience, Redaction, plants_for
from pack.schema import DetectorNote, DriverFact, EvidencePack, Fact, PackItem, SourceFreshness, Unit

# How each metric is spoken about, and which policy key governs what may be done about it (week 9).
METRIC_LABELS: dict[str, tuple[str, Unit, str]] = {
    "otif_rate": ("OTIF", "rate", "otif_drop"),
    "on_time_rate": ("on-time delivery", "rate", "otif_drop"),
    "fill_rate_weight": ("fill rate by weight", "rate", "fill_rate_drop"),
    "fill_rate_count": ("fill rate by case count", "rate", "fill_rate_drop"),
    "yield_variance_pct": ("yield variance", "points", "yield_variance"),
    "inventory_age_days": ("inventory age", "days", "inventory_at_risk"),
    "lb_at_risk_within_5d": ("pounds at risk within five days", "pounds", "inventory_at_risk"),
    "open_backlog_lines": ("open backlog lines", "count", "backlog_build"),
}
VOLUME_LABELS: dict[str, tuple[str, Unit]] = {
    "otif_rate": ("order lines", "count"),
    "on_time_rate": ("order lines", "count"),
    "fill_rate_count": ("order lines", "count"),
    "fill_rate_weight": ("ordered pounds", "pounds"),
    "yield_variance_pct": ("pounds into the line", "pounds"),
    "inventory_age_days": ("pounds on hand", "pounds"),
    "lb_at_risk_within_5d": ("pounds on hand", "pounds"),
    "open_backlog_lines": ("open lines", "count"),
}
SOURCE_TABLES = {"delivery metrics": "daily_otif_total", "inventory metrics": "daily_inventory_location",
                 "yield metrics": "daily_yield"}
SERIES_DAYS = 14


class PackError(RuntimeError):
    pass


def build_pack(frames: dict[str, pd.DataFrame], cfg: DetectConfig, policy: dict, run_date: date,
               series: list[SegmentSeries] | None = None, audience: Audience | None = None,
               gold_source: str = "standalone") -> tuple[EvidencePack, Redaction]:
    """One day's pack, built for one reader.

    The audience is a parameter rather than a filter applied afterwards: a pack built for everybody and then
    narrowed has already put the whole company into the prompt. The `Redaction` that comes back records
    what was withheld, for the ops log — the brief itself never mentions it.
    """
    reader = audience or DIRECTORY["vp"]
    plants = plants_for(reader)
    built = series if series is not None else build_series(frames, cfg)
    found = detect_range(built, cfg, run_date, run_date)
    ranked = incidents(found)
    allowed = [a for a in ranked if reader.may_see(a.segment)]
    redaction = Redaction(audience=reader.user, considered=len(ranked),
                          removed=len(ranked) - len(allowed),
                          segments=sorted({_segment_label(a.segment) for a in ranked
                                           if not reader.may_see(a.segment)}))
    cap = int(policy["limits"]["max_items_per_brief"])
    chosen = allowed[:cap]

    freshness, facts = _freshness(frames, policy, run_date), {}
    any_stale = any(f.stale for f in freshness)
    for f in freshness:
        facts[f.days_ref] = Fact(ref=f.days_ref, value=float(f.days_old), display=_plural(f.days_old, "day"),
                                 unit="days", label=f"how old the {f.source} are", source=f.source)

    items = []
    for index, anomaly in enumerate(chosen, start=1):
        item, item_facts = _item(f"I{index}", anomaly, frames, cfg, built, any_stale)
        items.append(item)
        facts.update(item_facts)

    return EvidencePack(
        run_date=run_date, generated_at=datetime.now(UTC), gold_source=gold_source, audience=reader.role,
        plants=sorted(plants or []), items=items, facts=facts, freshness=freshness, any_stale=any_stale,
        baseline_days=cfg.baseline_days, items_considered=len(allowed), items_cap=cap,
        is_holiday=cfg.is_holiday(run_date), calendar_note=_calendar_note(cfg, run_date),
        quiet_reason=None if items else _quiet_reason(found, ranked, reader)), redaction


def _calendar_note(cfg: DetectConfig, run_date: date) -> str | None:
    """A shutdown is the first thing a reader needs, not a discovery they make from the numbers.

    Without this the 4 July brief opens with OTIF at 34.6% and no explanation, and a reader either panics or
    learns to ignore the brief in July. Policy already knows the date is a holiday; the pack says so.
    """
    if not cfg.is_holiday(run_date):
        return None
    return (f"{run_date.isoformat()} falls inside a holiday window in policy.yaml. Movements on and around a "
            "shutdown are expected, and policy holds them below incident level.")


def _item(item_id: str, anomaly: Anomaly, frames: dict[str, pd.DataFrame], cfg: DetectConfig,
          series: list[SegmentSeries], any_stale: bool) -> tuple[PackItem, dict[str, Fact]]:
    direction: Literal["up", "down"] = "down" if anomaly.direction == "down" else "up"
    metric_label, unit, anomaly_type = METRIC_LABELS.get(
        anomaly.metric, (anomaly.metric.replace("_", " "), "count", "otif_drop"))
    volume_label, volume_unit = VOLUME_LABELS.get(anomaly.metric, ("records", "count"))
    segment_label = _segment_label(anomaly.segment)
    where = f"{segment_label} on {anomaly.metric_date.isoformat()}"
    facts: dict[str, Fact] = {}

    def add(suffix: str, value: float, fact_unit: Unit, label: str) -> str:
        ref = f"{item_id}.{suffix}"
        facts[ref] = Fact(ref=ref, value=value, display=_display(value, fact_unit), unit=fact_unit,
                          label=label, metric=anomaly.metric, metric_date=anomaly.metric_date,
                          source=_source_ref(anomaly))
        return ref

    value_ref = add("value", anomaly.value, unit, f"{metric_label} for {where}")
    expected_ref = (add("expected", anomaly.expected, unit,
                        f"what {metric_label} normally runs at for {segment_label}")
                    if anomaly.expected is not None else None)
    delta_ref = (add("change", anomaly.delta, _change_unit(unit),
                     f"how far {metric_label} moved from normal for {where}")
                 if anomaly.delta is not None else None)
    volume_ref = add("volume", anomaly.volume, volume_unit, f"{volume_label} behind {where}")

    result = attribute(frames, cfg, anomaly)
    drivers = _drivers(item_id, result, metric_label, unit, add)

    return PackItem(
        id=item_id, metric=anomaly.metric, metric_label=metric_label, grain=anomaly.grain,
        segment=anomaly.segment, segment_label=segment_label,
        severity="HIGH" if anomaly.severity is Severity.HIGH else "WARN", direction=direction,
        anomaly_type=anomaly_type, window_start=anomaly.window_start, window_end=anomaly.metric_date,
        value_ref=value_ref, expected_ref=expected_ref, delta_ref=delta_ref, volume_ref=volume_ref,
        detectors=[_detector_note(s, metric_label, unit) for s in anomaly.detectors],
        drivers=drivers, driver_note=result.note, policy_note=anomaly.suppressed_reason,
        series=_series(series, anomaly), stale=any_stale), facts


def _drivers(item_id: str, result: Attribution, metric_label: str, unit: Unit,
             add) -> list[DriverFact]:  # noqa: ANN001 - the closure above
    drivers = []
    for index, driver in enumerate(result.drivers, start=1):
        label = _segment_label(driver.segment)
        share_ref = add(f"driver{index}.share", driver.rate_effect_pct, "percent",
                        f"{label}'s share of the change in {metric_label}")
        value_ref = add(f"driver{index}.value", driver.window_value, unit,
                        f"{metric_label} for {label} over the window")
        baseline_ref = add(f"driver{index}.baseline", driver.baseline_value, unit,
                           f"what {metric_label} normally runs at for {label}")
        drivers.append(DriverFact(
            label=label, segment=driver.segment, share_pct=driver.rate_effect_pct, share_ref=share_ref,
            window_value=driver.window_value, baseline_value=driver.baseline_value, value_ref=value_ref,
            baseline_ref=baseline_ref, effect="mix" if abs(driver.mix_effect) > abs(driver.rate_effect)
            else "rate"))
    return drivers


def _detector_note(signal, metric_label: str, unit: Unit) -> DetectorNote:  # noqa: ANN001 - detect.models.Signal
    """Why this was flagged, in words, rendered in code. A brief explains the reason, never re-decides it."""
    detail = signal.detail
    if signal.detector == "threshold":
        basis, level, days = detail.get("basis"), detail.get("level"), int(detail.get("days_in_breach", 0))
        line = {"level": f"past the {_display(float(level), unit)} line",
                "abs": f"more than {_display(float(level), unit)} away from standard",
                "z": f"{level} standard deviations above its own normal",
                "drop": f"{_display(float(level), _change_unit(unit))} below its own normal"}.get(
                    str(basis), f"past {level}")
        text = f"{metric_label} {line} for {_plural(days, 'day')} running"
    elif signal.detector == "cusum":
        text = (f"a sustained shift {detail.get('direction')} since "
                f"{detail.get('change_point')}, held for {_plural(int(detail.get('run_days', 0)), 'day')}")
    else:
        text = (f"{abs(float(signal.z or 0)):.1f} standard deviations from its own "
                f"weekday-adjusted normal")
    return DetectorNote(detector=signal.detector, severity=signal.severity.label, detail=text)


def _series(series: list[SegmentSeries], anomaly: Anomaly) -> list[tuple[date, float]]:
    """The last fortnight of this segment's metric, for context. Not citable: the pack's facts are."""
    match = next((s for s in series if s.grain.name == anomaly.grain and s.segment == anomaly.segment), None)
    if match is None:
        return []
    start = anomaly.metric_date - timedelta(days=SERIES_DAYS)
    return [(day, round(float(match.values[i]), 4)) for i, day in enumerate(match.dates)
            if start <= day <= anomaly.metric_date]


def _freshness(frames: dict[str, pd.DataFrame], policy: dict, run_date: date) -> list[SourceFreshness]:
    """How old each source is, disclosed whether or not anything is wrong with it (principle 3)."""
    stale_after = int(policy["freshness"]["stale_after_days"])
    out = []
    for source, table in SOURCE_TABLES.items():
        frame = frames.get(table)
        if frame is None or frame.empty:
            continue
        latest = max(d for d in frame["metric_date"] if d <= run_date) if any(
            d <= run_date for d in frame["metric_date"]) else min(frame["metric_date"])
        days_old = (run_date - latest).days
        out.append(SourceFreshness(source=source, latest=latest, days_old=days_old,
                                   stale=days_old > stale_after,
                                   days_ref=f"freshness.{source.split()[0]}.days"))
    return out


def _quiet_reason(found: list[Anomaly], ranked: list[Anomaly], reader: Audience) -> str:
    """Why a brief is empty.

    "Nothing happened", "things moved but none of it was material" and "it happened somewhere you cannot see"
    are three different mornings, and a brief that renders all of them as silence teaches its reader that
    silence means nothing in particular.
    """
    if not ranked:
        return ("everything that moved was below the level policy calls material" if found
                else "no metric moved enough to report")
    if not [a for a in ranked if reader.may_see(a.segment)]:
        return "nothing to report for the plants in this brief"
    return "nothing above the reporting level"


def _segment_label(segment: dict[str, str]) -> str:
    if not segment:
        return "company-wide"
    order = ("plant", "location", "line", "product_group", "customer_no")
    parts = [segment[k] for k in order if k in segment] + [
        v for k, v in sorted(segment.items()) if k not in order]
    return " ".join(parts)


def _source_ref(anomaly: Anomaly) -> str:
    keys = "/".join(f"{k}={v}" for k, v in sorted(anomaly.segment.items())) or "ALL"
    return f"metrics:{anomaly.grain}:{keys}:{anomaly.metric_date.isoformat()}"


def _change_unit(unit: Unit) -> Unit:
    """A change in a rate is spoken of in percentage points, not as a rate."""
    return "rate_change" if unit == "rate" else unit


def _display(value: float, unit: Unit) -> str:
    """Round once, here. Every mention of this number afterwards is a copy of this string."""
    if unit == "rate":
        return f"{value * 100:.1f}%"
    if unit == "percent":
        return f"{value:.0f}%"
    if unit == "rate_change":
        return f"{value * 100:.1f} points"
    if unit == "points":
        return f"{value:.1f} points"
    if unit == "pounds":
        return f"{value:,.0f} lb"
    if unit == "days":
        return _plural(round(value), "day") if float(value).is_integer() else f"{value:.1f} days"
    if unit == "count":
        return f"{value:,.0f}"
    return str(value)


def _plural(count: float, noun: str) -> str:
    number = int(round(count))
    return f"{number} {noun}" + ("" if number == 1 else "s")
