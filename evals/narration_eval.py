"""Narrate a sample of days with the real model and report what the validator found.

This is the week-8 acceptance test, and it is deliberately run against days chosen by the calendar rather than
by us: whatever happened on those mornings is what the model has to write about, including the quiet ones and
the shutdown. Reported honestly, with the failures itemised:

- **citation coverage** — claims carrying a resolvable ref, over all claims. The hard gate: 100%.
- **first-pass validity** — briefs accepted without a correction. Below 100% is fine and interesting.
- **fallback rate** — briefs that ended up templated. Each one is listed with its reason.
- **cost and latency** — from `ops.llm_calls`, per brief, so nobody has to guess later.

Every complaint the validator raised is printed, because the point of the exercise is to find out what a model
does wrong when it is asked to be exact, not to produce a number that says 100%.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.config import get_settings  # noqa: E402
from detect.config import load_detect_config  # noqa: E402
from detect.run import build_series, load_frames  # noqa: E402
from llm.client import CallRecord, MemoryRecorder, build_client  # noqa: E402
from metrics.runner import load_policy  # noqa: E402
from narrate.run import NarrationResult, narrate  # noqa: E402
from pack.build import build_pack  # noqa: E402
from pack.permissions import audience_for  # noqa: E402


@dataclass
class DayResult:
    run_date: date
    audience: str
    items: int
    result: NarrationResult
    calls: list[CallRecord] = field(default_factory=list)

    @property
    def cost(self) -> float:
        return sum(c.cost_usd or 0.0 for c in self.calls)

    @property
    def latency_ms(self) -> int:
        return sum(c.latency_ms for c in self.calls)


def run_sample(days: list[date], users: list[str], out: Path | None = None) -> list[DayResult]:
    settings = get_settings()
    cfg = load_detect_config(settings.policy_file)
    policy = load_policy(settings.policy_file)
    frames = load_frames(settings.gold_dir.parent / "metrics")
    series = build_series(frames, cfg)

    results: list[DayResult] = []
    for day in days:
        for user in users:
            pack, _redaction = build_pack(frames, cfg, policy, day, series, audience_for(user))
            recorder = MemoryRecorder()
            client = build_client(settings, recorder)
            result = narrate(pack, client, batch_id=f"narration-eval-{day.isoformat()}")
            results.append(DayResult(run_date=day, audience=user, items=len(pack.items), result=result,
                                     calls=list(recorder.calls)))
            print(f"  {day} {user:<6} items={len(pack.items)} {result.source:<10} "
                  f"valid={result.validation.ok} coverage={result.validation.citation_coverage:.0%} "
                  f"${sum(c.cost_usd or 0 for c in recorder.calls):.4f}")
            for complaint in result.validation.complaints:
                print(f"      {complaint}")
    if out is not None:
        write_report(results, out)
        _save_drafts(results, out.parent / "results" / "rejected_drafts.json")
    return results


def _save_drafts(results: list[DayResult], path: Path) -> None:
    """The drafts the validator turned down, kept so a failure can be read rather than guessed at."""
    drafts = [{"date": r.run_date.isoformat(), "audience": r.audience,
               "draft": json.loads(draft.model_dump_json())}
              for r in results for draft in r.result.rejected]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(drafts, indent=2), encoding="utf-8")


def summarise(results: list[DayResult]) -> dict:
    claims = sum(r.result.validation.claims for r in results)
    cited = sum(r.result.validation.cited_claims for r in results)
    numbers = sum(r.result.validation.numbers_checked for r in results)
    first_pass = [r for r in results if r.result.source == "llm"]
    retried = [r for r in results if r.result.source == "llm_retry"]
    fell_back = [r for r in results if r.result.used_fallback]
    latencies = [r.latency_ms for r in results if r.latency_ms]
    return {
        "briefs": len(results),
        "citation_coverage": cited / claims if claims else 1.0,
        "claims": claims,
        "numbers_checked": numbers,
        "first_pass_valid": len(first_pass) / len(results) if results else 0.0,
        "corrected_then_valid": len(retried),
        "fallback_rate": len(fell_back) / len(results) if results else 0.0,
        "fallbacks": [{"date": r.run_date.isoformat(), "audience": r.audience,
                       "reason": r.result.fallback_reason} for r in fell_back],
        "complaints": [{"date": r.run_date.isoformat(), "audience": r.audience,
                        "complaint": str(c)}
                       for r in results for c in r.result.validation.complaints],
        "cost_usd_total": round(sum(r.cost for r in results), 4),
        "cost_usd_per_brief": round(sum(r.cost for r in results) / len(results), 5) if results else 0.0,
        "latency_ms_median": int(statistics.median(latencies)) if latencies else 0,
        "latency_ms_p95": int(sorted(latencies)[int(len(latencies) * 0.95) - 1]) if latencies else 0,
    }


def write_report(results: list[DayResult], path: Path) -> None:
    stats = summarise(results)
    settings = get_settings()
    lines = [
        "# Narration sample",
        "",
        f"`python evals/narration_eval.py` over **{stats['briefs']} briefs**, "
        f"model `{settings.llm_model}` via {settings.llm_provider}.",
        "Every number in every brief was checked against the evidence pack that produced it.",
        "",
        "| Measure | Result | Target |",
        "|---|---|---|",
        f"| Citation coverage | **{stats['citation_coverage']:.0%}** | 100% (hard gate) |",
        f"| Valid on the first attempt | **{stats['first_pass_valid']:.0%}** | reported |",
        f"| Corrected once, then valid | **{stats['corrected_then_valid']}** | reported |",
        f"| Fell back to the templated brief | **{stats['fallback_rate']:.0%}** | reported, with reasons |",
        f"| Claims checked | {stats['claims']} | |",
        f"| Numbers checked | {stats['numbers_checked']} | |",
        f"| Cost per brief | ${stats['cost_usd_per_brief']:.5f} | reported |",
        f"| Latency, median / p95 | {stats['latency_ms_median']} ms / "
        f"{stats['latency_ms_p95']} ms | reported |",
        "",
    ]
    if stats["fallbacks"]:
        lines += ["## Briefs that fell back", "", "| Date | Audience | Why |", "|---|---|---|"]
        lines += [f"| {f['date']} | {f['audience']} | {f['reason']} |" for f in stats["fallbacks"]]
        lines.append("")
    else:
        lines += ["No brief fell back to the template in this sample.", ""]
    if stats["complaints"]:
        lines += ["## What the validator caught", "",
                  "These are the corrections the model was sent. A brief only ships once its complaints are "
                  "empty, so every line here was fixed on the second attempt or replaced by the template.",
                  ""]
        lines += [f"- **{c['date']} ({c['audience']})** {c['complaint']}" for c in stats["complaints"]]
        lines.append("")
    else:
        lines += ["The validator raised no complaints in this sample.", ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="start", type=date.fromisoformat, default=date(2026, 3, 2))
    parser.add_argument("--days", type=int, default=20)
    parser.add_argument("--every", type=int, default=7, help="days between samples")
    parser.add_argument("--users", default="vp")
    parser.add_argument("--report", type=Path, default=REPO_ROOT / "evals" / "NARRATION.md")
    parser.add_argument("--json", dest="json_path", type=Path,
                        default=REPO_ROOT / "evals" / "results" / "narration.json")
    args = parser.parse_args()

    days = [args.start + timedelta(days=i * args.every) for i in range(args.days)]
    users = [u.strip() for u in args.users.split(",") if u.strip()]
    print(f"narrating {len(days) * len(users)} briefs ...")
    results = run_sample(days, users, args.report)

    stats = summarise(results)
    args.json_path.parent.mkdir(parents=True, exist_ok=True)
    args.json_path.write_text(json.dumps(stats, indent=2), encoding="utf-8", newline="\n")
    print(f"\ncitation coverage {stats['citation_coverage']:.0%} | first pass valid "
          f"{stats['first_pass_valid']:.0%} | fallback {stats['fallback_rate']:.0%} | "
          f"${stats['cost_usd_total']:.4f} total")
    return 0 if stats["citation_coverage"] == 1.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
