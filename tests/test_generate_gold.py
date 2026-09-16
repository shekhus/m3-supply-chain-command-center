"""The generator's promises: deterministic, self-checked, and the same world Project A produces."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from app.config import REPO_ROOT
from synth import anomaly_truth, generate_gold
from synth import config as C

SIBLING_GOLD = REPO_ROOT.parent / "m3-trusted-data-foundation" / "data" / "gold" / "fact_delivery.parquet"


def _fingerprint(df: pd.DataFrame) -> str:
    return str(pd.util.hash_pandas_object(df.astype(str), index=True).sum())


@pytest.fixture(scope="module")
def built() -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    return generate_gold.build()


def test_build_is_deterministic_for_a_seed(built: tuple[dict, dict]) -> None:
    gold, _ = built
    again, _ = generate_gold.build()
    assert {k: _fingerprint(v) for k, v in gold.items()} == {k: _fingerprint(v) for k, v in again.items()}
    assert _fingerprint(generate_gold.build(seed=C.SEED + 1)[0]["fact_delivery"]) != _fingerprint(
        gold["fact_delivery"])


def test_gold_carries_every_column_the_metric_layer_needs(built: tuple[dict, dict]) -> None:
    gold, _ = built
    assert set(generate_gold.TABLES) == set(gold)
    delivery = set(gold["fact_delivery"].columns)
    assert {"plant", "customer_no", "item_no", "product_group", "issue_date", "requested_date",
            "confirmed_delivery_date", "ordered_qty", "invoiced_qty", "ordered_weight_lb",
            "invoiced_weight_lb", "otif", "on_time", "in_full"} <= delivery
    assert {"plant", "line", "product_group", "run_date", "input_lb", "output_lb"} <= set(
        gold["fact_yield"].columns)
    assert {"location", "item_no", "lot_no", "snapshot_date", "on_hand_lb", "expiry_date"} <= set(
        gold["fact_inventory"].columns)


def test_every_seeded_anomaly_is_measurably_present(built: tuple[dict, dict]) -> None:
    _, metrics = built
    evidence, failures = anomaly_truth.self_check(metrics)
    assert not failures, failures
    assert set(evidence) == {a.anomaly_id for a in C.ANOMALIES}
    for a in C.ANOMALIES:
        assert evidence[a.anomaly_id]["window_rows"] > 0


def test_the_decoy_is_a_real_dip_so_seasonality_is_what_distinguishes_it(built: tuple[dict, dict]) -> None:
    _, metrics = built
    decoy = next(a for a in C.ANOMALIES if not a.expect_detection)
    evidence, _ = anomaly_truth.self_check(metrics)
    assert decoy.anomaly_id == "A5" and evidence["A5"]["delta"] < -0.05
    assert decoy.segment.get("plant") == "ALL"


def test_writing_produces_the_answer_key_and_a_manifest(tmp_path: Path, built: tuple[dict, dict]) -> None:
    gold, metrics = built
    evidence, _ = anomaly_truth.self_check(metrics)
    generate_gold.write(tmp_path, gold, metrics, evidence, C.SEED)

    key = json.loads((tmp_path / "ground_truth" / "anomalies.json").read_text(encoding="utf-8"))
    assert [a["anomaly_id"] for a in key["anomalies"]] == [a.anomaly_id for a in C.ANOMALIES]
    assert {"recall", "precision", "lead_time"} <= set(key["scoring"])
    assert all(a["observed"]["window_rows"] > 0 for a in key["anomalies"])
    assert [a["expect_detection"] for a in key["anomalies"]].count(False) == 1  # the decoy

    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["seed"] == C.SEED
    assert manifest["gold"]["fact_delivery"]["rows"] == len(gold["fact_delivery"])
    assert sorted(p.stem for p in (tmp_path / "gold").glob("*.parquet")) == sorted(generate_gold.TABLES)


def test_self_check_fails_loudly_when_an_anomaly_is_absent(built: tuple[dict, dict]) -> None:
    _, metrics = built
    flattened = {name: df.copy() for name, df in metrics.items()}
    lane = flattened["daily_otif_lane"]
    flattened["daily_otif_lane"] = lane.assign(otif_rate=0.95)  # erase A1 from the series
    _, failures = anomaly_truth.self_check(flattened)
    assert any(f.startswith("A1:") for f in failures)


@pytest.mark.skipif(not SIBLING_GOLD.exists(), reason="m3-trusted-data-foundation checkout with data/ absent")
def test_the_world_matches_project_a_row_for_row(built: tuple[dict, dict]) -> None:
    """Both repositories generate the same population from the same seed, so gold in A and B agree."""
    gold, _ = built
    theirs = pd.read_parquet(SIBLING_GOLD)
    ours = gold["fact_delivery"]
    shared = [c for c in theirs.columns if c in ours.columns]
    assert len(shared) > 20
    assert _fingerprint(ours[shared]) == _fingerprint(theirs[shared])
