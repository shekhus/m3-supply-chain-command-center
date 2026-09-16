"""Run the three detectors for one day (or a range) and print what a person would be asked to look at.

Equivalent of `make detect DATE=2026-06-15`. No LLM, no writes: this is the honest floor of the system — what
it knows before anything is narrated. `--all` shows the INFO findings too, with the reason each was capped,
which is the view to use when arguing about a threshold. Each incident is followed by its top drivers: the
finer segments the movement decomposes into, with the share of the performance change each one accounts for.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import create_engine  # noqa: E402

from app.config import get_settings  # noqa: E402
from detect.attribute import attribute  # noqa: E402
from detect.config import load_detect_config  # noqa: E402
from detect.models import Anomaly, Severity  # noqa: E402
from detect.run import build_series, detect_range, incidents, load_frames, window_for  # noqa: E402
from detect.series import SeriesError  # noqa: E402
from metrics.runner import MetricsError  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=date.fromisoformat, help="the day to run (default: the last day held)")
    parser.add_argument("--from", dest="from_date", type=date.fromisoformat, help="first day of a range")
    parser.add_argument("--to", dest="to_date", type=date.fromisoformat, help="last day of a range")
    parser.add_argument("--all", action="store_true", help="include INFO findings and why they were capped")
    parser.add_argument("--no-why", action="store_true", help="skip attribution (faster)")
    parser.add_argument("--parquet", action="store_true", help="read data/metrics instead of Postgres")
    args = parser.parse_args()

    settings = get_settings()
    try:
        cfg = load_detect_config(settings.policy_file)
        if args.parquet:
            frames = load_frames(settings.gold_dir.parent / "metrics")
        else:
            engine = create_engine(settings.database_url)
            frames = load_frames(engine)
            engine.dispose()
        first, last = window_for(frames)
        from_date = args.from_date or args.date or last
        to_date = args.to_date or args.date or last
        if from_date < first:
            print(f"detect: metrics start on {first}", file=sys.stderr)
            return 1
        found = detect_range(build_series(frames, cfg), cfg, from_date, to_date)
    except (MetricsError, SeriesError) as exc:
        print(f"detect: {exc}", file=sys.stderr)
        return 1

    shown = found if args.all else incidents(found)
    print(f"{from_date} .. {to_date}: {len(incidents(found))} incidents, {len(found)} findings")
    for anomaly in shown[:100]:
        print(_line(anomaly))
        if args.no_why or anomaly.severity < Severity.WARN:
            continue
        result = attribute(frames, cfg, anomaly)
        for driver in result.drivers:
            print(f"        why: {driver.label:<44} {driver.rate_effect_pct:6.1f}% of the change "
                  f"({driver.baseline_value:.4g} -> {driver.window_value:.4g})")
        if result.note:
            print(f"        why: {result.note}")
    if len(shown) > 100:
        print(f"  ... {len(shown) - 100} more")
    return 0


def _line(a: Anomaly) -> str:
    segment = "/".join(f"{k}={v}" for k, v in sorted(a.segment.items())) or "ALL"
    expected = f" expected {a.expected:.4g}" if a.expected is not None else ""
    detectors = ",".join(s.detector for s in a.detectors)
    reason = f"  [{a.suppressed_reason}]" if a.suppressed_reason else ""
    return (f"  {a.metric_date} {a.severity.label:<4} {a.metric:<22} {segment:<40} "
            f"{a.value:>10.4g}{expected} {a.direction:<4} rank {a.rank:7.2f} via {detectors}{reason}")


if __name__ == "__main__":
    raise SystemExit(main())
