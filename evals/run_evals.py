"""Every measured claim this project makes, gathered into one report.

Four evaluations, run in this order because each depends on the last being true:

1. **Detection** — replay over the whole history, scored against the seeded answer key.
2. **Narration** — real briefs through the real model, every number checked against its pack.
3. **Policy** — adversarial actions against the gate, including ones no drafter here can produce.
4. **Failure cases** — the eight rows of the plan's table, run as tests.

The report is generated, never written by hand, and it prints what failed as prominently as what passed. A
number in `REPORT.md` that nobody can reproduce with `make eval` is marketing.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from act.draft import draft_actions  # noqa: E402
from act.models import ActionStatus, OpenAction  # noqa: E402
from act.policy_gate import evaluate as gate  # noqa: E402
from app.config import get_settings  # noqa: E402
from detect.config import load_detect_config  # noqa: E402
from detect.run import build_series, load_frames  # noqa: E402
from metrics.runner import load_policy  # noqa: E402
from pack.build import build_pack  # noqa: E402
from pack.permissions import audience_for  # noqa: E402
from replay.run_replay import TARGETS, load_specs, replay, split_scores  # noqa: E402


def _days(value: float | None) -> str:
    if value is None:
        return "—"
    number = int(round(value))
    return f"{number} day" + ("" if number == 1 else "s")


@dataclass
class Section:
    name: str
    rows: list[tuple[str, str, str, bool]] = field(default_factory=list)   # measure, result, target, met

    @property
    def met(self) -> bool:
        return all(row[3] for row in self.rows)


def detection_section(world: tuple) -> tuple[Section, dict]:
    frames, cfg, policy, _series = world
    specs = load_specs(get_settings().ground_truth_dir / "anomalies.json")
    result = replay(frames, cfg, specs)
    tuned, held = split_scores(frames, cfg, specs, date(2026, 2, 28))
    card = result.card

    section = Section("Detection (replay over the full history)")
    section.rows = [
        ("Recall (WARN+)", f"{card.recall:.0%}", "≥ 90%", card.recall >= TARGETS["recall"]),
        ("Precision (HIGH events)", f"{card.precision:.0%}", "≥ 80%",
         card.precision >= TARGETS["precision"]),
        ("Attribution top-1", f"{card.attribution_accuracy:.0%}", "≥ 85%",
         card.attribution_accuracy >= TARGETS["attribution_accuracy"]),
        ("Holiday decoy raised HIGH", str(card.decoy_high_events), "0", card.decoy_high_events == 0),
        ("Median lead time", _days(card.median_lead_time), "reported", True),
        ("Held-out precision (never tuned on)", f"{held.card.precision:.0%}", "reported", True),
    ]
    return section, {"full": card.to_dict(), "tuned": tuned.card.to_dict(), "held_out": held.card.to_dict()}


def narration_section(days: int, every: int) -> tuple[Section, dict]:
    from evals.narration_eval import run_sample, summarise

    start = date(2026, 3, 2)
    results = run_sample([start + timedelta(days=i * every) for i in range(days)], ["vp"], None)
    stats = summarise(results)

    section = Section(f"Narration ({stats['briefs']} real briefs through the model)")
    section.rows = [
        ("Citation coverage", f"{stats['citation_coverage']:.0%}", "100% (hard gate)",
         stats["citation_coverage"] == 1.0),
        ("Valid on the first attempt", f"{stats['first_pass_valid']:.0%}", "reported", True),
        ("Fell back to the template", f"{stats['fallback_rate']:.0%}", "reported", True),
        ("Numbers checked", str(stats["numbers_checked"]), "—", True),
        ("Cost per brief", f"${stats['cost_usd_per_brief']:.5f}", "reported", True),
        ("Latency p95", f"{stats['latency_ms_p95']} ms", "reported", True),
    ]
    return section, stats


def policy_section(world: tuple) -> tuple[Section, dict]:
    """Adversarial: things no drafter here produces, which the gate still has to refuse."""
    frames, cfg, policy, series = world
    pack, _ = build_pack(frames, cfg, policy, date(2025, 9, 15), series, audience_for("vp"))
    drafted = draft_actions(pack, policy)

    attacks = {
        "a type the policy does not list": drafted[0].model_copy(update={"type": "update_m3"}),
        "a type not allowed for this anomaly": drafted[0].model_copy(update={"type": "investigation"}),
        "an anomaly type policy never heard of": drafted[0].model_copy(update={"anomaly_type": "price"}),
        "an item that is not in the brief": drafted[0].model_copy(update={"item_id": "I99"}),
    }
    blocked = {name: bool(gate([action], pack, policy).blocked)
               for name, action in attacks.items()}

    item = next(i for i in pack.items if i.id == drafted[0].item_id)
    duplicate = gate(drafted, pack, policy, [OpenAction(
        anomaly_type=drafted[0].anomaly_type, segment_label=item.segment_label, metric=item.metric,
        type=drafted[0].type, created_on=pack.run_date, external_ref="SC-0001")])
    suppressed = bool(duplicate.suppressed)
    clean = gate(drafted, pack, policy)
    never_approved = all(a.status is not ActionStatus.APPROVED for a in clean.all_actions)

    section = Section("Policy gate (adversarial)")
    section.rows = [(name, "blocked" if ok else "**ALLOWED**", "blocked", ok)
                    for name, ok in blocked.items()]
    section.rows.append(("a duplicate of an open ticket", "suppressed" if suppressed else "**ALLOWED**",
                         "suppressed", suppressed))
    section.rows.append(("the gate approving anything by itself", "never" if never_approved else "**YES**",
                         "never", never_approved))
    return section, {"blocked": blocked, "duplicate_suppressed": suppressed}


def failure_cases_section() -> tuple[Section, dict]:
    """The plan's eight cases, run as their own suite so the report cannot claim what the tests do not."""
    # No -q here: pyproject already sets one, and a second suppresses the summary line this parses.
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "tests/failure_cases"],
        cwd=REPO_ROOT, capture_output=True, text=True)
    passed = completed.returncode == 0
    summary_line = next((line.strip() for line in reversed(completed.stdout.splitlines())
                         if "passed" in line or "failed" in line), "no output")
    count = "".join(ch for ch in summary_line.split("passed")[0] if ch.isdigit()) or "?"

    section = Section("Failure cases (plan §3.6)")
    section.rows = [("The eight cases in the plan's table", f"{count} tests passed" if passed
                     else f"**FAILED** — {summary_line}", "all pass", passed)]
    return section, {"passed": passed, "output": summary_line}


def write_report(sections: list[Section], details: dict, path: Path) -> None:
    settings = get_settings()
    everything_met = all(s.met for s in sections)
    lines = [
        "# Evaluation report",
        "",
        f"Generated by `make eval` on {date.today().isoformat()}. Model: `{settings.llm_model}` via "
        f"{settings.llm_provider}. Every number here is reproducible with that command.",
        "",
        f"**{'All targets met.' if everything_met else 'SOME TARGETS NOT MET — see below.'}**",
        "",
    ]
    for section in sections:
        lines += [f"## {section.name}", "", "| Measure | Result | Target | Met |", "|---|---|---|---|"]
        lines += [f"| {m} | **{r}** | {t} | {'yes' if ok else '**NO**'} |" for m, r, t, ok in section.rows]
        lines.append("")

    lines += [
        "## What these numbers do not say",
        "",
        "- Detection precision counts every HIGH event with no seeded anomaly behind it as wrong. Some are "
        "real movements in a random world: the generator seeds six anomalies and promises nothing about the "
        "other 529 days. The number is a floor.",
        "- The detection floors in `policy.yaml` were tuned on the first half of the history. The held-out "
        "half was never used for tuning, and is reported separately for that reason.",
        "- Narration is measured on twenty days sampled by the calendar, not chosen. A different fortnight "
        "would give a different first-pass rate; the citation gate is the number that must not move.",
        "- Cost and latency come from `ops.llm_calls` and are what this model charged on the day.",
        "",
        "## How to reproduce",
        "",
        "```",
        "make synth-gold      # the world and its answer key",
        "make migrate metrics # gold and the metric tables",
        "make replay          # detection, scored against ground truth",
        "make eval            # this report",
        "```",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    (path.parent / "results" / "evals.json").parent.mkdir(parents=True, exist_ok=True)
    (path.parent / "results" / "evals.json").write_text(json.dumps(details, indent=2, default=str),
                                                        encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--narration-days", type=int, default=20)
    parser.add_argument("--every", type=int, default=7)
    parser.add_argument("--skip-narration", action="store_true",
                        help="skip the model calls (they cost money and need a key)")
    parser.add_argument("--report", type=Path, default=REPO_ROOT / "evals" / "REPORT.md")
    parser.add_argument("--strict", action="store_true", help="exit non-zero unless every target is met")
    args = parser.parse_args()

    settings = get_settings()
    cfg = load_detect_config(settings.policy_file)
    policy = load_policy(settings.policy_file)
    frames = load_frames(settings.gold_dir.parent / "metrics")
    world = (frames, cfg, policy, build_series(frames, cfg))

    sections, details = [], {}
    print("detection ...")
    section, detail = detection_section(world)
    sections.append(section)
    details["detection"] = detail

    if not args.skip_narration:
        print("narration ...")
        section, detail = narration_section(args.narration_days, args.every)
        sections.append(section)
        details["narration"] = detail

    print("policy gate ...")
    section, detail = policy_section(world)
    sections.append(section)
    details["policy"] = detail

    print("failure cases ...")
    section, detail = failure_cases_section()
    sections.append(section)
    details["failure_cases"] = detail

    write_report(sections, details, args.report)
    for section in sections:
        print(f"  {'ok  ' if section.met else 'MISS'} {section.name}")
    where = args.report.relative_to(REPO_ROOT) if args.report.is_relative_to(REPO_ROOT) else args.report
    print(f"wrote {where}")
    return 0 if (all(s.met for s in sections) or not args.strict) else 1


if __name__ == "__main__":
    raise SystemExit(main())
