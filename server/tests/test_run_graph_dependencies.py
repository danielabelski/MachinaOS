"""Triggered runs retain the full configuration of connected teammate agents.

Both event-listener and in-process deployment runs must select the same graph,
regardless of the order in which canvas edges were saved. These tests use only
in-memory graphs; no database, Temporal server, browser, or provider is needed.
"""

from __future__ import annotations

from copy import deepcopy
from itertools import permutations
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _node(node_id: str, node_type: str = "aiAgent") -> dict:
    return {"id": node_id, "type": node_type, "data": {"label": node_id}}


def _edge(source: str, target: str, handle: str = "input-main", *, key: str = "targetHandle") -> dict:
    return {"id": f"{source}-{target}-{handle}", "source": source, "target": target, key: handle}


@pytest.fixture
def graph_builders(monkeypatch):
    # services.temporal imports the supervisor's dependency-free cli.tcp module.
    # Make the project root available without constructing any runtime services.
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]))
    from temporalio import workflow
    from services.deployment.manager import DeploymentManager
    from services.temporal.trigger_listener_workflow import _build_run_graph

    monkeypatch.setattr(workflow, "logger", MagicMock())
    monkeypatch.setattr(workflow, "patched", lambda _patch_id: True)
    manager = object.__new__(DeploymentManager)
    return _build_run_graph, manager._get_downstream_nodes


@pytest.fixture(params=["listener", "deployment"])
def select_nodes(graph_builders, request):
    listener_builder, deployment_builder = graph_builders

    def select(nodes, edges):
        if request.param == "listener":
            run_nodes, _ = listener_builder(
                trigger_node_id="trigger", trigger_output={"message": "test"}, nodes=nodes, edges=edges,
            )
            return {node["id"] for node in run_nodes} - {"trigger"}
        return {node["id"] for node in deployment_builder("trigger", nodes, edges)}

    return select


@pytest.mark.parametrize("handle_key", ["targetHandle", "target_handle"])
@pytest.mark.parametrize("edge_order", list(permutations(range(4))))
def test_teammate_context_and_skills_survive_every_edge_order(select_nodes, handle_key, edge_order):
    nodes = [
        _node("trigger", "taskTrigger"), _node("lead", "orchestrator_agent"), _node("browser"),
        _node("context", "context"), _node("skill", "masterSkill"), _node("tool", "browser"),
    ]
    critical_edges = [
        _edge("context", "browser", "input-context", key=handle_key),
        _edge("skill", "browser", "input-skill", key=handle_key),
        _edge("browser", "lead", "input-teammates", key=handle_key),
        _edge("trigger", "lead", "input-task", key=handle_key),
    ]
    # The previous tools-only recovery pass preserved this tool while silently
    # dropping the earlier Context and Master Skill connections.
    edges = [critical_edges[index] for index in edge_order] + [
        _edge("tool", "browser", "input-tools", key=handle_key),
    ]

    assert select_nodes(nodes, edges) == {"lead", "browser", "context", "skill", "tool"}


def test_nested_teammates_and_tool_configuration_are_closed_transitively(select_nodes):
    nodes = [
        _node("trigger", "webhookTrigger"), _node("lead", "orchestrator_agent"), _node("browser"),
        _node("researcher"), _node("tool", "browser"), _node("context", "context"),
        _node("skill", "masterSkill"), _node("model", "geminiModel"), _node("memory", "simpleMemory"),
    ]
    # Dependencies occur before their consumers, including configuration for a
    # tool and a teammate of a teammate. A fixed number of recovery passes loses
    # some of these nodes as the team grows.
    edges = [
        _edge("memory", "model", "input-memory"),
        _edge("model", "tool", "input-model", key="target_handle"),
        _edge("skill", "researcher", "input-skill"),
        _edge("context", "researcher", "input-context", key="target_handle"),
        _edge("tool", "researcher", "input-tools"),
        _edge("researcher", "browser", "input-teammates", key="target_handle"),
        _edge("browser", "lead", "input-teammates"),
        _edge("trigger", "lead"),
    ]

    assert select_nodes(nodes, edges) == {node["id"] for node in nodes} - {"trigger"}


def test_configuration_and_runtime_cycles_terminate(select_nodes):
    nodes = [
        _node("trigger", "webhookTrigger"), _node("lead"), _node("output", "console"),
        _node("browser"), _node("context", "context"),
    ]
    edges = [
        _edge("context", "browser", "input-context"),
        _edge("browser", "context", "input-tools"),
        _edge("browser", "lead", "input-teammates"),
        _edge("trigger", "lead"), _edge("lead", "output"), _edge("output", "lead"),
    ]

    assert select_nodes(nodes, edges) == {"lead", "output", "browser", "context"}


def test_config_closure_preserves_trigger_and_component_boundaries(select_nodes):
    nodes = [
        _node("trigger", "webhookTrigger"), _node("lead"), _node("output", "console"),
        _node("browser"), _node("context", "context"),
        _node("sibling", "taskTrigger"), _node("sibling-output", "console"),
        _node("unrelated-agent"), _node("unrelated-output", "console"),
        _node("config-only-output", "console"), _node("ordinary-upstream", "console"),
    ]
    edges = [
        _edge("context", "browser", "input-context"),
        _edge("sibling", "browser", "input-tools"),
        _edge("sibling", "browser", "input-context"),
        _edge("browser", "lead", "input-teammates"),
        _edge("trigger", "lead"), _edge("lead", "output"),
        _edge("lead", "sibling"), _edge("sibling", "sibling-output"),
        _edge("unrelated-agent", "unrelated-output"),
        _edge("browser", "config-only-output"),
        _edge("ordinary-upstream", "lead"),
    ]

    # Configuration discovery must not execute a teammate's separate main-flow
    # branch or cross an independent trigger, even through input-tools.
    assert select_nodes(nodes, edges) == {"lead", "output", "browser", "context"}


def test_both_builders_preserve_source_order_and_inputs(graph_builders):
    listener_builder, deployment_builder = graph_builders
    nodes = [
        _node("context", "context"), _node("orphan"), _node("browser"),
        _node("trigger", "taskTrigger"), _node("lead", "orchestrator_agent"), _node("skill", "masterSkill"),
    ]
    edges = [
        _edge("context", "browser", "input-context"), _edge("skill", "browser", "input-skill"),
        _edge("browser", "lead", "input-teammates"), _edge("trigger", "lead", "input-task"),
        _edge("orphan", "orphan"),
    ]
    original_nodes, original_edges = deepcopy(nodes), deepcopy(edges)
    trigger_output = {"message": "do the task", "metadata": {"source": "test"}}
    original_output = deepcopy(trigger_output)

    run_nodes, run_edges = listener_builder(
        trigger_node_id="trigger", trigger_output=trigger_output, nodes=nodes, edges=edges,
    )
    deployment_nodes = deployment_builder("trigger", nodes, edges)

    assert [node["id"] for node in run_nodes] == ["context", "browser", "trigger", "lead", "skill"]
    assert [node["id"] for node in deployment_nodes] == ["context", "browser", "lead", "skill"]
    assert run_edges == edges[:-1]
    trigger = next(node for node in run_nodes if node["id"] == "trigger")
    assert trigger["_pre_executed"] is True
    assert trigger["_trigger_output"] == original_output
    assert nodes == original_nodes
    assert edges == original_edges
    assert trigger_output == original_output
    assert all(node is not original for node in run_nodes for original in nodes)
