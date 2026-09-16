"""Generate gold tables with seeded anomalies, and the answer key every later measurement is scored against.

    masters → events → gold → daily metrics → SELF-CHECK → parquet + ground_truth/anomalies.json

Standalone mode (plan B14): this repository needs gold without running m3-trusted-data-foundation. The world
model, events and gold builders are the same code and the same seed as that generator, so both produce the
identical population; `tests/test_generate_gold.py` checks that when a sibling checkout is present.

The self-check is a hard gate. Each seeded anomaly is recomputed at the grain it was planted at and compared
with a baseline period; if one is absent or points the wrong way, generation writes **nothing** to
ground_truth/ and exits non-zero. Everything downstream — detection recall, attribution accuracy, lead time —
is scored against that file, so a ground truth that lies would quietly invalidate the whole evaluation.

The daily metrics written here are the generator's own pandas recomputation, kept for the self-check and as an
independent check on the SQL metric layer (metrics/sql). The system itself reads the SQL tables, never these.
"""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from . import anomaly_truth, events, world
from . import config as C
from . import gold as gold_mod

TABLES = ("fact_delivery", "fact_inventory", "fact_yield", "dim_customer", "dim_item")


def _rng(seed: int, stream: int) -> np.random.Generator:
    """One independent stream per stage, so adding a stage cannot shift earlier draws."""
    return np.random.default_rng([seed, stream])


def build(seed: int = C.SEED) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    """(gold tables, daily metric series) for the whole window. No I/O."""
    customers = world.build_customers(_rng(seed, 1))
    items = world.build_items(_rng(seed, 2))
    lines = events.generate_order_lines(_rng(seed, 3), customers, items)
    inventory = events.generate_inventory(_rng(seed, 4), items)
    yields = events.generate_yield(_rng(seed, 5), items)
    gold = gold_mod.build_gold(lines, customers, items, inventory, yields)
    return gold, gold_mod.daily_metrics(gold)


def write(out: Path, gold: dict[str, pd.DataFrame], metrics: dict[str, pd.DataFrame],
          evidence: dict[str, dict], seed: int) -> None:
    for sub in ("gold", "metrics", "ground_truth"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    for name, df in gold.items():
        df.to_parquet(out / "gold" / f"{name}.parquet", index=False)
    for name, df in metrics.items():
        df.to_parquet(out / "metrics" / f"{name}.parquet", index=False)
    anomaly_truth.write_anomalies(out / "ground_truth", evidence)
    manifest = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "seed": seed,
        "company": C.COMPANY_NAME,
        "window": {"start": str(C.START_DATE), "end": str(C.END_DATE)},
        "gold": {name: {"rows": int(len(df)), "columns": list(df.columns)} for name, df in gold.items()},
        "metrics": {name: int(len(df)) for name, df in metrics.items()},
        "anomalies": [a.anomaly_id for a in C.ANOMALIES],
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str) + "\n",
                                       encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="data", help="output directory")
    ap.add_argument("--seed", type=int, default=C.SEED)
    ap.add_argument("--clean", action="store_true", help="wipe the output directory first")
    args = ap.parse_args(argv)

    out = Path(args.out)
    if args.clean and out.exists():
        shutil.rmtree(out)

    print(f"{C.COMPANY_NAME} gold — seed {args.seed}, window {C.START_DATE} .. {C.END_DATE}")
    gold, metrics = build(args.seed)
    print("  gold:    " + ", ".join(f"{name} {len(df):,}" for name, df in gold.items()))
    print("  metrics: " + ", ".join(f"{name} {len(df):,}" for name, df in metrics.items()))

    evidence, failures = anomaly_truth.self_check(metrics)
    for a in C.ANOMALIES:
        seen = evidence.get(a.anomaly_id, {})
        detail = (f"window {seen['window_value']} vs baseline {seen['baseline_value']} "
                  f"(delta {seen['delta']})" if "window_value" in seen else seen.get("error", "?"))
        flag = "decoy" if not a.expect_detection else "seeded"
        print(f"  {a.anomaly_id} {flag:<6} {a.metric:<20} {detail}")
    if failures:
        print("\nSELF-CHECK FAILED — ground truth not written:")
        for f in failures:
            print(f"  {f}")
        return 1

    write(out, gold, metrics, evidence, args.seed)
    print(f"  self-check: all {len(C.ANOMALIES)} seeded anomalies verified present in the metrics")
    print(f"  wrote {out}/gold, {out}/metrics, {out}/ground_truth/anomalies.json, {out}/manifest.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
