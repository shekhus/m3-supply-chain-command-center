"""The HTTP layer: who may build a brief, who may see it, and who may approve.

The console trusts the API for every permission decision, so these are the tests that keep that trust
honest — above all: **the audience comes from the key, never from the request.** A plant manager who knows
the date must not be able to read the leadership brief by typing its thread id.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine

VP_KEY = "test-owner-vp"
PLANT_KEY = "test-owner-plt01"
VIEWER_KEY = "test-viewer-vp"
ANALYST_KEY = "test-analyst-vp"
LANE_DAY = date(2025, 9, 15)


@pytest.fixture
def client(pg_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """An app wired to a fresh database, with keys that carry an audience each."""
    from db.migrate import migrate

    if not (os.environ.get("DATA_DIR", "data")):
        pytest.skip("no data directory")
    migrate(pg_url)
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("API_KEYS", f"owner:{VP_KEY},owner:{PLANT_KEY},viewer:{VIEWER_KEY},"
                                   f"analyst:{ANALYST_KEY}")
    monkeypatch.setenv("API_AUDIENCES", f"{VP_KEY}:vp,{PLANT_KEY}:plt01,{VIEWER_KEY}:vp,"
                                        f"{ANALYST_KEY}:vp")
    monkeypatch.setenv("LLM_PROVIDER", "none")      # the fallback brief: no model in an API test
    monkeypatch.setenv("METRICS_SOURCE", "parquet")  # a fresh database has the schema, not the metrics

    import service
    service.load_world.cache_clear()
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
    service.load_world.cache_clear()


def _engine(pg_url: str) -> Engine:
    return create_engine(pg_url)


def _run(client: TestClient, key: str, run_date: date = LANE_DAY) -> dict:
    response = client.post("/brief/run", params={"run_date": run_date.isoformat()},
                           headers={"X-API-Key": key})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.postgres
def test_a_brief_is_built_and_waits(client: TestClient) -> None:
    brief = _run(client, VP_KEY)
    assert brief["status"] == "PENDING_APPROVAL" and brief["awaiting_approval"]
    assert brief["summary"] and brief["items"]
    assert any(a["status"] == "PROPOSED" for a in brief["actions"])
    assert brief["thread_id"].endswith(":vp")


@pytest.mark.postgres
def test_the_audience_comes_from_the_key_not_the_request(client: TestClient) -> None:
    """The whole permission model rests on this."""
    vp = _run(client, VP_KEY)
    plant = _run(client, PLANT_KEY)

    assert vp["audience"] == "vp" and plant["audience"] == "plt01"
    assert plant["thread_id"] != vp["thread_id"]
    assert "PLT-02" not in str(plant["items"]), "a PLT-01 key must not receive PLT-02's numbers"


@pytest.mark.postgres
def test_one_key_cannot_read_another_audiences_brief(client: TestClient) -> None:
    """Knowing the date is not permission. Without this, the pack filtering was for nothing."""
    vp = _run(client, VP_KEY)
    response = client.get(f"/brief/{vp['thread_id']}", headers={"X-API-Key": PLANT_KEY})
    assert response.status_code == 403
    assert "may only see briefs for 'plt01'" in response.json()["detail"]


@pytest.mark.postgres
def test_reading_a_brief_does_not_advance_it(client: TestClient) -> None:
    built = _run(client, VP_KEY)
    read = client.get(f"/brief/{built['thread_id']}", headers={"X-API-Key": VIEWER_KEY})
    assert read.status_code == 200
    assert read.json()["awaiting_approval"] is True
    assert read.json()["executed"] == []


@pytest.mark.postgres
def test_only_an_owner_may_decide(client: TestClient) -> None:
    built = _run(client, VP_KEY)
    action = next(a for a in built["actions"] if a["status"] == "PROPOSED")
    body = {"decisions": [{"action_id": action["id"], "verdict": "approve"}]}

    for key in (VIEWER_KEY, ANALYST_KEY):
        refused = client.post(f"/brief/{built['thread_id']}/decide", json=body,
                              headers={"X-API-Key": key})
        assert refused.status_code == 403, key


@pytest.mark.postgres
def test_a_viewer_may_not_build_a_brief(client: TestClient) -> None:
    response = client.post("/brief/run", headers={"X-API-Key": VIEWER_KEY})
    assert response.status_code == 403


@pytest.mark.postgres
def test_approving_executes_and_returns_the_ticket(client: TestClient) -> None:
    built = _run(client, VP_KEY)
    action = next(a for a in built["actions"] if a["status"] == "PROPOSED")

    response = client.post(f"/brief/{built['thread_id']}/decide",
                           json={"decisions": [{"action_id": action["id"], "verdict": "approve"}]},
                           headers={"X-API-Key": VP_KEY})
    assert response.status_code == 200, response.text
    after = response.json()
    executed = next(a for a in after["actions"] if a["id"] == action["id"])
    assert executed["status"] == "EXECUTED" and executed["external_ref"]
    assert executed["decided_by"] == "vp"


@pytest.mark.postgres
def test_an_edit_is_what_reaches_the_tracker(client: TestClient) -> None:
    built = _run(client, VP_KEY)
    action = next(a for a in built["actions"] if a["status"] == "PROPOSED")

    response = client.post(
        f"/brief/{built['thread_id']}/decide",
        json={"decisions": [{"action_id": action["id"], "verdict": "edit",
                             "title": "Call the carrier about C000031",
                             "body": "Missed collections on the lane.", "note": "narrowed the ask"}]},
        headers={"X-API-Key": VP_KEY})
    assert response.status_code == 200, response.text
    edited = next(a for a in response.json()["actions"] if a["id"] == action["id"])
    assert edited["title"] == "Call the carrier about C000031"
    assert edited["status"] == "EXECUTED"


@pytest.mark.postgres
def test_rejecting_executes_nothing(client: TestClient) -> None:
    built = _run(client, VP_KEY)
    action = next(a for a in built["actions"] if a["status"] == "PROPOSED")

    response = client.post(f"/brief/{built['thread_id']}/decide",
                           json={"decisions": [{"action_id": action["id"], "verdict": "reject",
                                                "note": "known issue"}]},
                           headers={"X-API-Key": VP_KEY})
    rejected = next(a for a in response.json()["actions"] if a["id"] == action["id"])
    assert rejected["status"] == "REJECTED" and rejected["external_ref"] is None


@pytest.mark.postgres
def test_an_unknown_key_gets_nothing(client: TestClient) -> None:
    assert client.post("/brief/run", headers={"X-API-Key": "not-a-key"}).status_code == 401
    assert client.post("/brief/run").status_code == 401
    assert client.get("/brief/2025-09-15:vp").status_code == 401


@pytest.mark.postgres
def test_a_brief_that_was_never_built_is_a_404(client: TestClient) -> None:
    response = client.get("/brief/2099-01-01:vp", headers={"X-API-Key": VP_KEY})
    assert response.status_code == 404


@pytest.mark.postgres
def test_a_second_run_for_the_same_day_returns_the_same_brief(client: TestClient) -> None:
    """The cron may fire twice; a person must not get two different briefs for one morning."""
    first = _run(client, VP_KEY)
    second = _run(client, VP_KEY)
    assert first["thread_id"] == second["thread_id"]
    assert [a["id"] for a in first["actions"]] == [a["id"] for a in second["actions"]]


def test_healthz_needs_no_key() -> None:
    from app.main import app

    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
