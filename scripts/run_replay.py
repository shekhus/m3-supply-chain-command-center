"""Replay the detectors over history and score them against the answer key. Equivalent of `make replay`.

    make replay FROM=2025-03-01 TO=2026-08-31

Writes `evals/REPLAY.md` — committed, because it is the set of numbers this project is allowed to claim — and
the full event detail to `evals/results/replay.json`, which is generated and not tracked.
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
from detect.config import load_detect_config  # noqa: E402
from detect.run import load_frames  # noqa: E402
from detect.series import SeriesError  # noqa: E402
from metrics.runner import MetricsError  # noqa: E402
from replay.run_replay import load_specs, replay, split_scores, summarise, write_report  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="from_date", type=date.fromisoformat, help="first day scored")
    parser.add_argument("--to", dest="to_date", type=date.fromisoformat, help="last day scored")
    parser.add_argument("--parquet", action="store_true", help="read data/metrics instead of Postgres")
    parser.add_argument("--report", type=Path, default=REPO_ROOT / "evals" / "REPLAY.md")
    parser.add_argument("--json", dest="json_path", type=Path,
                        default=REPO_ROOT / "evals" / "results" / "replay.json")
    parser.add_argument("--strict", action="store_true", help="exit non-zero unless every target is met")
    parser.add_argument("--split", type=date.fromisoformat, default=date(2026, 2, 28),
                        help="last day of the window the policy was tuned on; the rest is held out")
    args = parser.parse_args()

    settings = get_settings()
    try:
        cfg = load_detect_config(settings.policy_file)
        specs = load_specs(settings.ground_truth_dir / "anomalies.json")
        if args.parquet:
            frames = load_frames(settings.gold_dir.parent / "metrics")
        else:
            engine = create_engine(settings.database_url)
            frames = load_frames(engine)
            engine.dispose()
        result = replay(frames, cfg, specs, args.from_date, args.to_date)
        split = split_scores(frames, cfg, specs, args.split) if args.split else None
    except (MetricsError, SeriesError, FileNotFoundError) as exc:
        print(f"replay: {exc}", file=sys.stderr)
        return 1

    write_report(result, cfg, args.report, args.json_path, split, specs)
    print(summarise(result))
    print(f"wrote {args.report.relative_to(REPO_ROOT)} and {args.json_path.relative_to(REPO_ROOT)}")
    for finding in result.card.findings:
        if finding.expect_detection and not finding.detected:
            print(f"  MISSED {finding.anomaly_id} ({finding.metric})", file=sys.stderr)
    if args.strict and not all(result.met_targets.values()):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
