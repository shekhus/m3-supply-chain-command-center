"""The approval flow, as a graph that can be paused and resumed after a restart.

    build_pack → narrate → draft_actions → policy_gate → ⟪interrupt⟫ → execute → record

**Why a graph here and plain code in Project A.** Project A's pipeline is fixed — profile, map, validate,
publish — and a function call expresses that perfectly. This one has a pause in the middle that has to survive
a process ending: a brief goes out at 06:00, somebody approves an action at 14:00 from a different machine,
and the run continues from exactly where it stopped. That is a checkpointed state machine, and writing one by
hand means writing serialisation, resumption and a step ledger — which is what LangGraph already is. Smallest
abstraction that fits, in both directions.

**`interrupt_before=["execute"]` is the whole point.** The graph is compiled so that it *cannot* reach the
node that touches the outside world without a person resuming it. Not a flag the execute node checks — a stop
before the node runs at all (principle 5). A test proves the pause survives the process: the run is started,
the compiled graph and its connection are thrown away, and a new one resumes and executes.

**State is plain JSON.** Packs, briefs and actions are dumped to dicts on the way in and rebuilt on the way
out, because a checkpoint that cannot be read by a different process is not a checkpoint. The durable business
record — what went out, who approved it, what it became — is written to `ops.*` by the `record` node; the
checkpointer's tables are the paused machine, not the archive.

**Narration's retry lives inside the narrate node**, not as a graph edge looping back to the model. The plan's
diagram shows validate as its own node; keeping the loop where the call is means one implementation of "one
correction, then the template" instead of the same policy spread across two places. The validation result is
carried in state either way, so the console and the ops log see it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Literal, TypedDict

import psycopg
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from psycopg.rows import dict_row
from sqlalchemy.engine import make_url

from act.draft import draft_actions, key_for
from act.models import Action, ActionStatus, Decision, OpenAction
from act.policy_gate import check_decision, evaluate
from narrate.contract import Brief
from narrate.run import NarrationResult
from pack.schema import EvidencePack

CHECKPOINT_SCHEMA = "graph_checkpoints"
Status = Literal["PENDING_APPROVAL", "EXECUTED", "REJECTED", "EMPTY", "FAILED"]


class BriefState(TypedDict, total=False):
    """Everything the run needs to survive a restart. Dicts, not objects, on purpose."""

    thread_id: str
    run_date: str
    audience: str
    pack: dict
    brief: dict
    narration: dict
    actions: list[dict]
    gate: dict
    decisions: list[dict]
    executed: list[dict]
    status: Status
    error: str | None


@dataclass
class GraphDeps:
    """What the nodes need from the outside world, injected so a test can supply its own."""

    build_pack: Callable[[date, str], tuple[EvidencePack, Any]]
    policy: dict
    narrate: Callable[[EvidencePack], NarrationResult]
    execute: Callable[[Action], tuple[ActionStatus, str | None]]
    open_actions: Callable[[date], list[OpenAction]] = field(default=lambda _d: [])
    record: Callable[[BriefState], None] = field(default=lambda _s: None)


def build_graph(deps: GraphDeps) -> StateGraph:
    graph = StateGraph(BriefState)

    def build_pack_node(state: BriefState) -> BriefState:
        pack, _redaction = deps.build_pack(date.fromisoformat(state["run_date"]), state["audience"])
        return {"pack": pack.model_dump(mode="json")}

    def narrate_node(state: BriefState) -> BriefState:
        pack = EvidencePack.model_validate(state["pack"])
        result = deps.narrate(pack)
        return {"brief": result.brief.model_dump(mode="json"), "narration": result.to_dict()}

    def draft_node(state: BriefState) -> BriefState:
        pack = EvidencePack.model_validate(state["pack"])
        actions = draft_actions(pack, deps.policy)
        return {"actions": [a.model_dump(mode="json") for a in actions]}

    def gate_node(state: BriefState) -> BriefState:
        pack = EvidencePack.model_validate(state["pack"])
        actions = [Action.model_validate(a) for a in state.get("actions", [])]
        result = evaluate(actions, pack, deps.policy, deps.open_actions(pack.run_date))
        status: Status = "PENDING_APPROVAL" if result.allowed else "EMPTY"
        return {"actions": [a.model_dump(mode="json") for a in result.all_actions],
                "gate": result.to_dict(), "status": status}

    def execute_node(state: BriefState) -> BriefState:
        """Only reachable once a person has resumed the run, and only for what they approved."""
        decisions = {d["action_id"]: d for d in state.get("decisions", [])}
        updated, executed = [], []
        for raw in state.get("actions", []):
            action = Action.model_validate(raw)
            decision = decisions.get(action.id)
            if action.status is not ActionStatus.PROPOSED or decision is None:
                updated.append(action.model_dump(mode="json"))
                continue
            if decision["verdict"] == "reject":
                updated.append(action.with_status(
                    ActionStatus.REJECTED, decided_by=decision["decided_by"],
                    decided_at=datetime.now(UTC)).model_dump(mode="json"))
                continue
            edited = action.model_copy(update={
                "title": decision.get("title") or action.title,
                "body": decision.get("body") or action.body,
                "decided_by": decision["decided_by"], "decided_at": datetime.now(UTC)})
            check_decision(edited, decision["verdict"], deps.policy)   # policy still applies at approval
            status, external_ref = deps.execute(edited)
            done = edited.with_status(status, external_ref=external_ref)
            updated.append(done.model_dump(mode="json"))
            executed.append({"action_id": done.id, "status": status.value, "external_ref": external_ref})

        any_executed = any(e["status"] == ActionStatus.EXECUTED.value for e in executed)
        return {"actions": updated, "executed": executed,
                "status": "EXECUTED" if any_executed else "REJECTED"}

    def record_node(state: BriefState) -> BriefState:
        deps.record(state)
        return {}

    graph.add_node("build_pack", build_pack_node)
    graph.add_node("narrate", narrate_node)
    graph.add_node("draft_actions", draft_node)
    graph.add_node("policy_gate", gate_node)
    graph.add_node("execute", execute_node)
    graph.add_node("record", record_node)

    graph.add_edge(START, "build_pack")
    graph.add_edge("build_pack", "narrate")
    graph.add_edge("narrate", "draft_actions")
    graph.add_edge("draft_actions", "policy_gate")
    graph.add_edge("policy_gate", "execute")
    graph.add_edge("execute", "record")
    graph.add_edge("record", END)
    return graph


@contextmanager
def checkpointer(database_url: str) -> Iterator[PostgresSaver]:
    """A saver on its own schema, so the graph's tables never collide with the business ones."""
    dsn = make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row,
                         options=f"-c search_path={CHECKPOINT_SCHEMA}") as conn:
        saver = PostgresSaver(conn)
        saver.setup()
        yield saver


def _config(thread_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id}}


def thread_id_for(run_date: date, audience: str) -> str:
    """One thread per day per reader, so a second run for the same morning resumes rather than duplicates."""
    return f"{run_date.isoformat()}:{audience}"


@dataclass
class RunView:
    """What a console or an API needs to show a paused run."""

    thread_id: str
    run_date: date
    audience: str
    status: Status
    next_nodes: tuple[str, ...]
    brief: Brief | None
    pack: EvidencePack | None
    actions: list[Action]
    gate: dict | None
    narration: dict | None
    executed: list[dict]

    @property
    def awaiting_approval(self) -> bool:
        return "execute" in self.next_nodes

    @property
    def pending(self) -> list[Action]:
        return [a for a in self.actions if a.status is ActionStatus.PROPOSED]


def start_run(saver: PostgresSaver, deps: GraphDeps, run_date: date, audience: str) -> RunView:
    """Run up to the approval pause. Nothing has touched the outside world when this returns."""
    compiled = build_graph(deps).compile(checkpointer=saver, interrupt_before=["execute"])
    thread = thread_id_for(run_date, audience)
    initial: BriefState = {"thread_id": thread, "run_date": run_date.isoformat(), "audience": audience,
                           "decisions": [], "executed": [], "status": "PENDING_APPROVAL", "error": None}
    compiled.invoke(initial, _config(thread))
    return _view(compiled, thread)


def decide(saver: PostgresSaver, deps: GraphDeps, thread_id: str, decisions: list[Decision]) -> RunView:
    """Apply a person's decisions and let the run continue into `execute`.

    A fresh `compile` on purpose: this is normally a different process from the one that paused, which is the
    property the interrupt exists to provide.
    """
    compiled = build_graph(deps).compile(checkpointer=saver, interrupt_before=["execute"])
    compiled.update_state(_config(thread_id),
                          {"decisions": [d.model_dump(mode="json") for d in decisions]})
    compiled.invoke(None, _config(thread_id))
    return _view(compiled, thread_id)


def load_run(saver: PostgresSaver, deps: GraphDeps, thread_id: str) -> RunView:
    compiled = build_graph(deps).compile(checkpointer=saver, interrupt_before=["execute"])
    return _view(compiled, thread_id)


def _view(compiled: Any, thread_id: str) -> RunView:  # noqa: ANN401 - CompiledStateGraph
    snapshot = compiled.get_state(_config(thread_id))
    state: BriefState = snapshot.values
    pack = EvidencePack.model_validate(state["pack"]) if state.get("pack") else None
    brief = Brief.model_validate(state["brief"]) if state.get("brief") else None
    return RunView(
        thread_id=thread_id,
        run_date=date.fromisoformat(state["run_date"]), audience=state["audience"],
        status=state.get("status", "PENDING_APPROVAL"), next_nodes=tuple(snapshot.next),
        brief=brief, pack=pack,
        actions=[Action.model_validate(a) for a in state.get("actions", [])],
        gate=state.get("gate"), narration=state.get("narration"), executed=state.get("executed", []))


def open_actions_from(actions: list[Action], pack: EvidencePack) -> list[OpenAction]:
    """Turn a run's actions into the open-action shape the gate suppresses against."""
    out = []
    for action in actions:
        if not action.is_open:
            continue
        _anomaly_type, metric, segment = key_for(action, pack).split("|")
        out.append(OpenAction(anomaly_type=action.anomaly_type, segment_label=segment, metric=metric,
                              type=action.type, created_on=pack.run_date,
                              external_ref=action.external_ref, status=action.status))
    return out
