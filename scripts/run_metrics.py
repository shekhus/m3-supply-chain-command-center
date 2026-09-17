"""Load gold (standalone) and compute metrics.daily_* from metrics/sql. Equivalent of `make metrics`.

Idempotent: every metric file merges by its own key, so running the same window twice changes nothing. With
--from/--to it recomputes just that range, which is what the daily run does.
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
from db.migrate import MigrationError, require_postgres  # noqa: E402
from metrics.runner import MetricsError, run  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="from_date", type=date.fromisoformat, help="first metric date")
    parser.add_argument("--to", dest="to_date", type=date.fromisoformat, help="last metric date")
    parser.add_argument("--skip-load", action="store_true", help="do not reload gold from data/gold")
    # For loading a deployment from a workstation: the repo's .env wins over the shell by design, so a URL
    # has to be passed explicitly rather than exported (docs/RUNBOOK.md, "Rebuild the metrics").
    parser.add_argument("--database-url", default=None,
                        help="override the configured database (use the platform's public URL)")
    args = parser.parse_args()
    settings = get_settings()
    try:
        database_url = args.database_url or settings.database_url
        require_postgres(database_url)
        engine = create_engine(database_url)
        source = "external" if args.skip_load else settings.gold_source
        result = run(engine, settings.policy_file, settings.gold_dir, source, args.from_date, args.to_date)
        engine.dispose()
    except (MigrationError, MetricsError) as exc:
        print(f"metrics: {exc}", file=sys.stderr)
        return 1
    if result.gold_rows:
        print("gold loaded: " + ", ".join(f"{k} {v:,}" for k, v in result.gold_rows.items()))
    print(f"window {result.from_date} .. {result.to_date} (run {result.run_id})")
    for name, rows in result.metric_rows.items():
        print(f"  {name:<28} {rows:,} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
