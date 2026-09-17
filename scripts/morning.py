"""The morning run: build each audience's brief, deliver it, and say what happened.

This is what a scheduler calls (`make brief`, or a Railway cron). It runs in-process rather than posting to
its own API, because a cron that authenticates to its own service needs a key in a second place, and the only
thing the HTTP hop would add here is a way to be down.

Exit codes are for the scheduler: 0 when every brief was built, 1 when any failed. A delivery failure is not
a failed run — the brief exists, the console has it, and the failure is on `ops.tool_calls` where the ops page
will show it. Losing a brief is a silent failure; failing to email one is a noisy inconvenience.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import create_engine  # noqa: E402

import service  # noqa: E402
from app.config import get_settings  # noqa: E402
from deliver.send import deliver  # noqa: E402


def audiences() -> list[str]:
    """`MORNING_AUDIENCES=vp,plt01,plt02` — who gets a brief, each one filtered to what they may see."""
    raw = os.environ.get("MORNING_AUDIENCES", "vp")
    return [name.strip() for name in raw.split(",") if name.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=date.fromisoformat, default=None,
                        help="the day to brief on (default: today)")
    parser.add_argument("--audiences", default=None, help="comma-separated; overrides MORNING_AUDIENCES")
    parser.add_argument("--no-deliver", action="store_true", help="build the briefs but send nothing")
    args = parser.parse_args()

    settings = get_settings()
    engine = create_engine(settings.database_url)
    run_date = args.date or date.today()
    who = [a.strip() for a in args.audiences.split(",")] if args.audiences else audiences()

    failures = 0
    for audience in who:
        try:
            view = service.run_brief(engine, run_date, audience)
        except Exception as exc:                      # one audience failing must not stop the others
            failures += 1
            print(f"{run_date} {audience}: FAILED to build — {type(exc).__name__}: {exc}", file=sys.stderr)
            continue

        items = len(view.brief.items) if view.brief else 0
        # ASCII separators: a scheduler's log is read in whatever console the host gives it, and a Windows
        # one renders a middle dot as a question mark.
        print(f"{run_date} {audience}: {view.status.lower().replace('_', ' ')} | {items} item(s) | "
              f"{len(view.pending)} waiting | written by "
              f"{(view.narration or {}).get('source', 'unknown')}")

        if args.no_deliver:
            continue
        for result in deliver(view, settings.console_url, engine):
            marker = "ok" if result.ok else "FAILED"
            print(f"    {result.channel}: {marker}" + (f" - {result.detail}" if result.detail else ""))

    engine.dispose()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
