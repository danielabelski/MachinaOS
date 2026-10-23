"""The public creation boundary owns identity and the runtime birth policy."""
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from routers import agents
from tests.nodes._agent_builder_harness import (
    builder_fixture, database_fixture, save_graph, saved,
)


def request(owner="owner"):
    return SimpleNamespace(state=SimpleNamespace(user_id=owner))


@pytest.fixture
def creation(database, builder, monkeypatch):
    monkeypatch.setattr(agents, "container", SimpleNamespace(database=lambda: database))
    monkeypatch.setenv("NEW_AGENT_RUNTIME_V1_ENABLED", "true")
    return builder


@pytest.mark.asyncio
async def test_fresh_creation_is_atomic_and_retry_returns_same_ids(database, creation):
    await save_graph(database, {"nodes": [], "edges": [], "owner_id": "owner"})
    body = agents.CreateAgent(workflow_id="7", node_type="aiAgent", mutation_id=uuid4(),
        parameters={"runtime_behavior_version": 0, "model": "gemini-2.5-flash"})
    first, retry = await agents.create_agent(body, request()), await agents.create_agent(body, request())
    assert first["applied"] is True
    assert retry["applied"] is False
    assert retry["node_ids"] == first["node_ids"]
    graph = await saved(database)
    assert {item["id"] for item in graph["nodes"]} == set(first["node_ids"].values())
    # Receipts replay the same announcement if the original response was lost.
    assert len(creation.frames) == 2
    assert creation.frames[0] == creation.frames[1]
    params = await database.get_node_parameters(first["node_ids"]["agent"])
    assert params["runtime_behavior_version"] == 1
    assert params["model"] == "gemini-2.5-flash"


@pytest.mark.asyncio
async def test_gate_off_never_accepts_client_upgrade(database, creation, monkeypatch):
    monkeypatch.setenv("NEW_AGENT_RUNTIME_V1_ENABLED", "false")
    await save_graph(database, {"nodes": [], "edges": [], "owner_id": "owner"})
    result = await agents.create_agent(agents.CreateAgent(workflow_id="7", node_type="aiAgent",
        mutation_id=uuid4(), parameters={"runtime_behavior_version": 1}), request())
    assert "runtime_behavior_version" not in await database.get_node_parameters(result["node_ids"]["agent"])


@pytest.mark.asyncio
async def test_receipt_never_bypasses_owner_check(database, creation):
    graph = {"nodes": [], "edges": [], "owner_id": "owner"}
    await save_graph(database, graph)
    body = agents.CreateAgent(workflow_id="7", node_type="aiAgent", mutation_id=uuid4())
    await agents.create_agent(body, request())
    with pytest.raises(HTTPException) as caught:
        await agents.create_agent(body, request("stranger"))
    assert caught.value.status_code == 404
    assert len(creation.frames) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,initial", [("shell", {}), ("browser_agent", {}), ("aiAgent", {"temperature": "invalid"})])
async def test_rejected_creation_has_no_nodes_parameters_or_edges(database, creation, kind, initial):
    await save_graph(database, {"nodes": [], "edges": []})
    with pytest.raises(HTTPException) as caught:
        await agents.create_agent(agents.CreateAgent(workflow_id="7", node_type=kind,
            mutation_id=uuid4(), parameters=initial), request())
    assert caught.value.status_code == 400
    assert (await saved(database))["nodes"] == []
    assert (await saved(database))["edges"] == []
    assert creation.frames == []


@pytest.mark.asyncio
@pytest.mark.parametrize("original", [{}, {"runtime_behavior_version": 0}, {"runtime_behavior_version": 1}])
async def test_save_and_inplace_mutation_preserve_saved_identity(database, original):
    node_id = "7:aiAgent:1"
    assert await database.save_node_parameters(node_id, original)
    assert await database.save_node_parameters(node_id, {"runtime_behavior_version": 1 - original.get("runtime_behavior_version", 0), "model": "next"})
    params = await database.get_node_parameters(node_id)
    assert params.get("runtime_behavior_version") == original.get("runtime_behavior_version")
    def mutate(value):
        value["runtime_behavior_version"] = 1 - original.get("runtime_behavior_version", 0)
        value["model"] = "last"
        return value
    await database.mutate_node_parameters_atomic(node_id, mutate)
    params = await database.get_node_parameters(node_id)
    assert params.get("runtime_behavior_version") == original.get("runtime_behavior_version")
    assert params["model"] == "last"
