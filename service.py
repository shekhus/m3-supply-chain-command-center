"""Wiring: the pieces, assembled once, for the API, the console and the cron to share.

Everything the graph needs from the outside world is built here — the pack builder, the narrator, the
executor, the open-action query and the recorder — so a caller asks for a brief rather than assembling six
collaborators and getting one of them subtly wrong. The graph itself stays ignorant of Postgres, Groq and
Jira; this module is the only place that knows all three exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import Engine, create_engine

from act import store
from act.jira_adapter import build_executor
from act.models import Action, Decision, OpenAction
from app.config import Settings, get_settings
from detect.config import DetectConfig, load_detect_config
from detect.run import build_series, load_frames
from detect.series import SegmentSeries
from graph import BriefState, GraphDeps, RunView, checkpointer, decide, load_run, start_run
from llm.client import DbRecorder, build_client
from metrics.runner import load_policy
from narrate.run import NarrationResult, narrate
from pack.build import build_pack
from pack.permissions import Audience, audience_for
from pack.schema import EvidencePack


@dataclass
class World:
    """The metrics and settings every run of a deployment shares."""

    frames: dict[str, pd.DataFrame]
    series: list[SegmentSeries]
    cfg: DetectConfig
    policy: dict
    settings: Settings


@lru_cache(maxsize=2)
def load_world(source: str | None = None) -> World:
    """Built once per process: loading the metrics and their baselines takes seconds and never changes."""
    settings = get_settings()
    cfg = load_detect_config(settings.policy_file)
    policy = load_policy(settings.policy_file)
    frames = load_frames(_metrics_source(settings, source))
    return World(frames=frames, series=build_series(frames, cfg), cfg=cfg, policy=policy,
                 settings=settings)


def _metrics_source(settings: Settings, source: str | None) -> Engine | Path:
    if source == "parquet":
        return settings.gold_dir.parent / "metrics"
    return create_engine(settings.database_url)


def build_deps(world: World, engine: Engine, run_id: int | None = None,
               with_model: bool = True) -> GraphDeps:
    """Assemble what the graph's nodes call. The only place the real collaborators are chosen."""
    client = build_client(world.settings, DbRecorder(engine)) if with_model else None
    executor = build_executor(world.settings, engine, run_id)

    def build(run_date: date, who: str) -> tuple[EvidencePack, Any]:
        return build_pack(world.frames, world.cfg, world.policy, run_date, world.series,
                          audience_for(who), gold_source=world.settings.gold_source)

    def narrate_pack(pack: EvidencePack) -> NarrationResult:
        return narrate(pack, client, batch_id=f"brief-{pack.run_date.isoformat()}-{pack.audience}")

    def open_for(run_date: date) -> list[OpenAction]:
        return store.open_actions(engine, before=run_date)

    def record(state: BriefState) -> None:
        record_state(engine, state)

    return GraphDeps(build_pack=build, policy=world.policy, narrate=narrate_pack, execute=executor,
                     open_actions=open_for, record=record)


def record_state(engine: Engine, state: BriefState) -> None:
    """Write the durable record. Idempotent, because it runs at the pause and again after execution."""
    if not state.get("pack"):
        return
    pack = EvidencePack.model_validate(state["pack"])
    run_id = store.start_run(engine, pack.run_date, state["audience"])
    narration = state.get("narration") or {}
    narrated_by = str(narration.get("source", "unknown"))

    store.save_brief(engine, run_id, state["thread_id"], pack.run_date, state["audience"],
                     state["pack"], state.get("brief") or {}, narrated_by)
    actions = [Action.model_validate(a) for a in state.get("actions", [])]
    segments = {item.id: (item.segment_label, item.metric) for item in pack.items}
    store.save_actions(engine, run_id, state["thread_id"], pack.run_date, actions, segments)
    store.finish_run(engine, run_id, state.get("status", "PENDING_APPROVAL"), len(pack.items),
                     narrated_by, narration.get("fallback_reason"), state.get("error"))


def run_brief(engine: Engine, run_date: date, audience: str, source: str | None = None,
              with_model: bool = True) -> RunView:
    """Build a morning's brief and stop before anything leaves the building."""
    world = load_world(source)
    run_id = store.start_run(engine, run_date, audience)
    deps = build_deps(world, engine, run_id, with_model)
    with checkpointer(world.settings.database_url) as saver:
        return start_run(saver, deps, run_date, audience)


def apply_decisions(engine: Engine, thread_id: str, decisions: list[Decision],
                    source: str | None = None, with_model: bool = True) -> RunView:
    """Resume a paused run with a person's decisions, usually from a different process than paused it."""
    world = load_world(source)
    deps = build_deps(world, engine, None, with_model)
    store.save_decisions(engine, decisions)
    with checkpointer(world.settings.database_url) as saver:
        return decide(saver, deps, thread_id, decisions)


def view_run(engine: Engine, thread_id: str, source: str | None = None,
             with_model: bool = True) -> RunView:
    """Read a paused run back, for a console to show. Looking at something never advances it."""
    world = load_world(source)
    deps = build_deps(world, engine, None, with_model)
    with checkpointer(world.settings.database_url) as saver:
        return load_run(saver, deps, thread_id)


def audience_of(user: str) -> Audience:
    return audience_for(user)
