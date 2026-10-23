"""Private profile metadata is atomic, immutable in meaning and never migrated."""
from copy import deepcopy

import pytest
from sqlalchemy import select

from models.browser_profiles import BrowserProfileRow, BrowserProfileReservation
from nodes.browser._profiles import ProfileStore, ProfileError
from services.browser_resources import (
    BrowserResourceError, allocate_new_browser_resources, validate_browser_resource_bindings,
    validate_saved_browser_resources, assert_browser_resource_access, browser_runtime_guidance,
)


def bundle(version=1):
    graph = {"nodes": [
        {"id": "7:aiAgent:1", "type": "aiAgent", "data": {"label": "Agent"}},
        {"id": "7:browser:1", "type": "browser", "data": {"label": "Private"}},
    ], "edges": [{"source": "7:browser:1", "target": "7:aiAgent:1", "targetHandle": "input-tools"}]}
    return graph, {"7:aiAgent:1": {"runtime_behavior_version": version}, "7:browser:1": {}}


async def allocate(database, graph, params):
    async with database.get_session() as session:
        await allocate_new_browser_resources(session, "7", "owner", graph, params, {"7:browser:1"})
        await session.flush()
        await validate_browser_resource_bindings(session, "7", "owner", graph, params)
        await session.commit()


async def test_new_metadata_allocates_private_identity_without_files(real_database, monkeypatch):
    import nodes.browser._profiles as profiles
    monkeypatch.setattr(profiles, "profile_dir", lambda *_: pytest.fail("metadata allocation created files"))
    graph, params = bundle()
    await allocate(real_database, graph, params)
    profile_id = params["7:browser:1"]["profile_id"]
    profile = await ProfileStore(real_database).get("owner", profile_id)
    assert profile.kind == "agent" and profile.runtime_version == 1
    assert profile.agent_node_id == "7:aiAgent:1" and profile.browser_node_id == "7:browser:1"
    assert profile.workflow_id == "7"
    async with real_database.get_session() as session:
        row = await session.get(BrowserProfileRow, profile_id)
        assert row.workflow_id is None
        reservation = await session.get(BrowserProfileReservation, profile_id)
        assert reservation.runtime_version == 1
    assert graph["nodes"][1]["data"]["private_browser_owner"] == {"agent_node_id": "7:aiAgent:1", "runtime_version": 1}
    with pytest.raises(ProfileError):
        await ProfileStore(real_database).get("other-owner", profile_id)


async def test_legacy_allocation_is_a_noop(real_database):
    graph, params = bundle(0)
    before = deepcopy(params)
    await allocate(real_database, graph, params)
    assert params == before
    async with real_database.get_session() as session:
        assert not (await session.execute(select(BrowserProfileReservation))).scalars().all()


@pytest.mark.parametrize("kind", ["legacy", "foreign_agent", "foreign_browser", "foreign_owner", "unreserved"])
async def test_binding_cannot_transfer_private_identity(real_database, kind):
    graph, params = bundle()
    await allocate(real_database, graph, params)
    principal = "owner"
    if kind == "legacy":
        params["7:aiAgent:1"] = {}
    elif kind == "foreign_agent":
        graph["nodes"][0]["id"] = "copy"
        graph["edges"][0]["target"] = "copy"
        params["copy"] = params.pop("7:aiAgent:1")
    elif kind == "foreign_browser":
        graph["nodes"][1]["id"] = "copy"
        graph["edges"][0]["source"] = "copy"
        params["copy"] = params.pop("7:browser:1")
    elif kind == "foreign_owner":
        principal = "other"
    else:
        params["7:browser:1"] = {}
    async with real_database.get_session() as session:
        with pytest.raises(BrowserResourceError):
            await validate_browser_resource_bindings(session, "7", principal, graph, params)


async def test_reservation_missing_fails_before_credentials_or_viewer_open(real_database):
    async with real_database.get_session() as session:
        session.add(BrowserProfileRow(id="broken", owner_id="owner", name="Broken", kind="agent"))
        await session.commit()
    with pytest.raises(ProfileError, match="reservation"):
        await ProfileStore(real_database).get("owner", "broken")


async def test_resource_provenance_cannot_be_reassigned(real_database):
    graph, params = bundle()
    await allocate(real_database, graph, params)
    async with real_database.get_session() as session:
        reservation = await session.get(BrowserProfileReservation, params["7:browser:1"]["profile_id"])
        reservation.agent_node_id = "another-agent"
        with pytest.raises(ValueError, match="immutable"):
            await session.flush()
        await session.rollback()


async def test_metadata_rolls_back_with_owning_graph_transaction(real_database):
    graph, params = bundle()
    async with real_database.get_session() as session:
        await allocate_new_browser_resources(session, "7", "owner", graph, params, {"7:browser:1"})
        await session.flush()
        await session.rollback()
    async with real_database.get_session() as session:
        assert not (await session.execute(select(BrowserProfileRow))).scalars().all()
        assert not (await session.execute(select(BrowserProfileReservation))).scalars().all()


async def test_no_browser_and_legacy_empty_profile_are_noop_without_database():
    await validate_saved_browser_resources(object(), "7", "owner", {"nodes": [], "edges": []}, {})
    graph, params = bundle(0)
    await validate_saved_browser_resources(object(), "7", "owner", graph, params)


async def test_v1_without_durable_adapter_fails_closed():
    graph, params = bundle(1)
    with pytest.raises(BrowserResourceError):
        await validate_saved_browser_resources(object(), "7", "owner", graph, params)
    with pytest.raises(BrowserResourceError):
        await assert_browser_resource_access(object(), profile_id="p", principal="owner",
            workflow_id="7", browser_node_id="b", runtime_version=1)


def test_readiness_guidance_publishes_normalized_captured_state():
    assert "needs_observation=true" in browser_runtime_guidance({"needs_observation": True})
    assert "needs_observation=false" in browser_runtime_guidance({"needs_observation": False})
    assert "needs_observation=unknown" in browser_runtime_guidance()


def test_browser_skill_variant_requires_explicit_fresh_creation(monkeypatch):
    from types import SimpleNamespace
    from services.browser_agent_recipe import browser_agent_additions
    monkeypatch.setattr("services.agent_runtime_policy.new_agent_runtime_enabled", lambda: True)
    monkeypatch.setattr("services.skill_defaults.v1_default", lambda name: SimpleNamespace(instructions="PRIVATE V1 SKILL"))
    def instructions(**kwargs):
        nodes = browser_agent_additions(**kwargs).nodes
        return next(n.params["skills_config"]["browser-skill"]["instructions"] for n in nodes if n.type == "masterSkill")
    assert instructions() == "PRIVATE V1 SKILL"
    assert instructions(create_agent=False, fresh_skills=True) == "PRIVATE V1 SKILL"
    assert instructions(create_agent=False) != "PRIVATE V1 SKILL"
