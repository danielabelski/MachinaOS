"""Native replay and same-controller worker upgrade for graph dependencies.

Run in a clean subprocess: the shared pytest fixtures stub Temporal and core
services. This gate uses an isolated native dev server and no application DB,
providers, or browser processes.
"""
from __future__ import annotations

import asyncio
import importlib
import os
import subprocess
import sys
import types
from pathlib import Path
from uuid import uuid4

import pytest
from temporalio import activity, workflow
from temporalio.api.enums.v1 import EventType, IndexedValueType
from temporalio.api.operatorservice.v1 import AddSearchAttributesRequest
from temporalio.service import RPCError, RPCStatusCode
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker


# Pin the actual worker before this fix; HEAD will move after it is committed.
_PRE_FIX_COMMIT = "41bca81aad25430803bc1de02e6205d094123a1c"
_MODULES = ("trigger_listener_workflow", "polling_trigger_workflow", "workflow_control_workflow")
_poll_events: list[dict] = []


@activity.defn(name="poll.googleGmailReceive.v1")
async def poll(payload: dict) -> dict:
    events = list(_poll_events)
    _poll_events.clear()
    return {"events": events, "seen_ids": [*payload["seen_ids"], *(event["id"] for event in events)]}


@activity.defn(name="broadcast_trigger_status_activity")
async def broadcast(_payload: dict) -> None:
    pass


@activity.defn(name="evaluate_trigger_filter_activity")
async def evaluate_filter(_payload: dict) -> bool:
    return True


@workflow.defn(name="MachinaWorkflow", sandboxed=False)
class GraphProbe:
    @workflow.run
    async def run(self, payload: dict) -> dict:
        # Observe the native child's recorded input without running any nodes.
        return payload


def _load_old(stem: str):
    package = importlib.import_module("services.temporal")
    name = "services.temporal." + stem
    source = subprocess.check_output(
        ["git", "show", f"{_PRE_FIX_COMMIT}:server/services/temporal/{stem}.py"],
        cwd=Path(__file__).parents[3], encoding="utf-8",
    )
    module = types.ModuleType(name)
    module.__package__ = "services.temporal"
    module.__file__ = f"git:{_PRE_FIX_COMMIT}:{stem}"
    sys.modules[name] = module
    setattr(package, stem, module)
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module


def _restore(modules: dict) -> None:
    package = importlib.import_module("services.temporal")
    for stem, module in modules.items():
        sys.modules[module.__name__] = module
        setattr(package, stem, module)


def _registration(trigger: str, slug: str) -> dict:
    nodes = [
        {"id": "push", "type": "webhookTrigger"},
        {"id": "poll", "type": "googleGmailReceive"},
        {"id": "lead", "type": "aiAgent"},
        {"id": "browser", "type": "browser_agent"},
        {"id": "context", "type": "context"},
        {"id": "skill", "type": "skill"},
        {"id": "nested-context", "type": "context"},
        {"id": "tool", "type": "browser"},
        {"id": "unrelated", "type": "aiAgent"},
    ]
    # The dependency edges precede the edge discovering their teammate. The
    # previous single pass drops Context and Skill, then restores only tools.
    edges = [
        {"source": "context", "target": "browser", "targetHandle": "input-context"},
        {"source": "nested-context", "target": "skill", "targetHandle": "input-context"},
        {"source": "skill", "target": "browser", "targetHandle": "input-skill"},
        {"source": "browser", "target": "lead", "targetHandle": "input-teammates"},
        {"source": "tool", "target": "browser", "targetHandle": "input-tools"},
        {"source": "push", "target": "lead", "targetHandle": "input-main"},
        {"source": "poll", "target": "lead", "targetHandle": "input-main"},
    ]
    polling = trigger == "poll"
    event_type = "com.opencompany.graph-replay.received"
    return {
        "listener_id": trigger,
        "workflow_type": "PollingTriggerWorkflow" if polling else "TriggerListenerWorkflow",
        "trigger_node_id": trigger,
        "event_type": event_type,
        "listener_args": {
            "trigger_node_id": trigger, "trigger_label": trigger,
            "node_type": "googleGmailReceive" if polling else "webhookTrigger",
            "workflow_slug": slug, "workflow_id": None, "data_scope_id": "test-scope",
            "graphVersion": 2, "generation": 1,
            "event_type": event_type,
            "filter_params": {"poll_interval": 1} if polling else {},
            "nodes": nodes, "edges": edges,
        },
    }


def _assert_graph(payload: dict, trigger: str, *, fixed: bool) -> None:
    identifiers = {trigger, "lead", "browser", "tool"}
    if fixed:
        identifiers.update({"context", "skill", "nested-context"})
    assert {node["id"] for node in payload["nodes"]} == identifiers
    expected = _registration(trigger, payload["workflow_slug"])["listener_args"]
    assert payload["edges"] == [
        edge for edge in expected["edges"] if edge["source"] in identifiers and edge["target"] in identifiers
    ]
    assert payload["graphVersion"] == 2
    assert payload["generation"] == 1
    assert payload["data_scope_id"] == "test-scope"
    firing_node = next(node for node in payload["nodes"] if node["id"] == trigger)
    assert firing_node["_pre_executed"] is True
    assert firing_node["_trigger_output"]["body"] == trigger + " payload"


async def _child_result(client, identifier: str) -> dict:
    async def wait():
        while True:
            handle = client.get_workflow_handle(identifier)
            try:
                await handle.describe()
            except RPCError as exc:
                if exc.status != RPCStatusCode.NOT_FOUND:
                    raise
                await asyncio.sleep(0.025)
            else:
                return await handle.result()
    return await asyncio.wait_for(wait(), timeout=20)


async def _wait_registered(handle) -> None:
    async def wait():
        while set((await handle.query("status"))["triggers"]) != {"push", "poll"}:
            await asyncio.sleep(0.025)
    await asyncio.wait_for(wait(), timeout=10)


def _starts(history) -> list:
    return [
        event.start_child_workflow_execution_initiated_event_attributes
        for event in history.events
        if event.event_type == EventType.EVENT_TYPE_START_CHILD_WORKFLOW_EXECUTION_INITIATED
    ]


async def gate(cli: str, control_version: int) -> None:
    current = {stem: importlib.import_module("services.temporal." + stem) for stem in _MODULES}
    old = {stem: _load_old(stem) for stem in _MODULES}
    # Exercise real poll timers quickly; the same test-only floor applies to
    # both workers so upgrading changes only the graph construction code.
    for modules in (old, current):
        modules["workflow_control_workflow"]._MIN_POLL_INTERVAL_S = 1
        modules["polling_trigger_workflow"]._MIN_POLL_INTERVAL_S = 1
    token = uuid4().hex
    controller_id, slug, queue = "graph-controller-" + token, "graph-" + token, "graph-replay-" + token
    triggers = {trigger: _registration(trigger, slug) for trigger in ("push", "poll")}
    activities = [poll, broadcast, evaluate_filter]
    definitions = [current["workflow_control_workflow"].WorkflowControlWorkflow, GraphProbe]
    old_outputs, old_child_histories = {}, {}
    try:
        async with await WorkflowEnvironment.start_local(dev_server_existing_path=cli, ip="127.0.0.1", port=None) as env:
            client = env.client
            await client.operator_service.add_search_attributes(AddSearchAttributesRequest(
                namespace=client.namespace,
                search_attributes={"ControlEventTypes": IndexedValueType.INDEXED_VALUE_TYPE_KEYWORD_LIST},
            ))
            async with Worker(
                client, task_queue=queue,
                workflows=[old["workflow_control_workflow"].WorkflowControlWorkflow, GraphProbe],
                activities=activities, max_cached_workflows=0,
            ):
                handle = await client.start_workflow(
                    "WorkflowControlWorkflow", {
                        "triggers": triggers, "execution_control_version": control_version,
                        "controller_workflow_id": controller_id, "generation": 1,
                    }, id=controller_id, task_queue=queue,
                )
                original_run = handle.first_execution_run_id
                await _wait_registered(handle)
                await handle.signal("on_event", {
                    "id": "push-before", "type": triggers["push"]["event_type"], "data": {"body": "push payload"},
                })
                _poll_events.append({"id": "poll-before", "body": "poll payload"})
                for trigger in triggers:
                    identifier = f"{slug}-{trigger}-{trigger}-before"
                    output = await _child_result(client, identifier)
                    _assert_graph(output, trigger, fixed=False)
                    old_outputs[identifier] = output
                    old_child_histories[identifier] = await client.get_workflow_handle(identifier).fetch_history()
                await handle.execute_update("set_control_state", {
                    "state": "paused", "revision": 1, "generation": 1,
                } if control_version else "paused")
                old_history = await handle.fetch_history()

            # Genuine pre-fix native history must replay with the new builder;
            # no patch marker is required for changing child input alone.
            _restore(current)
            replay = await Replayer(workflows=definitions).replay_workflow(old_history)
            assert replay.replay_failure is None
            async with Worker(
                client, task_queue=queue, workflows=definitions,
                activities=activities, max_cached_workflows=0,
            ):
                assert (await handle.describe()).run_id == original_run
                await handle.execute_update("set_control_state", {
                    "state": "running", "revision": 2, "generation": 1,
                } if control_version else "running")
                await handle.signal("on_event", {
                    "id": "push-after", "type": triggers["push"]["event_type"], "data": {"body": "push payload"},
                })
                _poll_events.append({"id": "poll-after", "body": "poll payload"})
                child_histories = list(old_child_histories.values())
                for trigger in triggers:
                    identifier = f"{slug}-{trigger}-{trigger}-after"
                    output = await _child_result(client, identifier)
                    _assert_graph(output, trigger, fixed=True)
                    assert output["context_execution_id"] == identifier
                    if control_version:
                        assert output["execution_control_version"] == 1
                        assert output["controller_workflow_id"] == controller_id
                    child_histories.append(await client.get_workflow_handle(identifier).fetch_history())
                # Upgrade must neither rewrite old children nor change the
                # controller run. Reset is used only for final test cleanup.
                assert (await handle.describe()).run_id == original_run
                for identifier, expected in old_outputs.items():
                    assert await client.get_workflow_handle(identifier).result() == expected
                    history = await client.get_workflow_handle(identifier).fetch_history()
                    assert history.to_json() == old_child_histories[identifier].to_json()
                await handle.signal("reset")
                await asyncio.wait_for(handle.result(), timeout=10)
                final_history = await handle.fetch_history()

            assert len(_starts(old_history)) == 2
            starts = _starts(final_history)
            assert len(starts) == 4
            assert all(start.workflow_type.name == "MachinaWorkflow" for start in starts)
            for trigger in triggers:
                assert [start.workflow_id for start in starts if start.workflow_id.startswith(f"{slug}-{trigger}-")] == [
                    f"{slug}-{trigger}-{trigger}-before", f"{slug}-{trigger}-{trigger}-after",
                ]
            assert not any(
                event.event_type in (
                    EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CONTINUED_AS_NEW,
                    EventType.EVENT_TYPE_WORKFLOW_TASK_FAILED,
                ) for event in final_history.events
            )
            for history in [final_history, *child_histories]:
                replay = await Replayer(workflows=definitions).replay_workflow(history)
                assert replay.replay_failure is None
    finally:
        _poll_events.clear()
        _restore(current)


@pytest.mark.parametrize("control_version", [0, 1], ids=["legacy", "execution-control-v1"])
def test_native_graph_old_history_and_same_controller_upgrade(control_version):
    cli = os.environ.get("TEMPORAL_TEST_CLI")
    if not cli or not Path(cli).is_file():
        pytest.skip("Set TEMPORAL_TEST_CLI to an existing native Temporal CLI binary")
    completed = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "tests.temporal.test_run_graph_replay", cli, str(control_version)],
        cwd=Path(__file__).parents[2], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180, check=False,
    )
    assert completed.returncode == 0, completed.stdout.decode(errors="replace")[-16000:]


if __name__ == "__main__":
    asyncio.run(gate(sys.argv[1], int(sys.argv[2])))
