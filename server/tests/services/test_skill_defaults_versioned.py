from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.skill_defaults import new_skill_parameters, v1_default
from services.skill_runtime import _load_authoritative


def test_v1_default_does_not_replace_legacy_cache():
    from services.skill_loader import get_skill_loader
    loader = get_skill_loader()
    legacy = loader.load_skill("browser-skill")
    assert legacy is not None
    original = legacy.instructions
    versioned = v1_default("browser-skill", legacy)
    assert "snapshot` before navigation" in versioned.instructions
    assert versioned is not legacy
    assert loader.load_skill("browser-skill").instructions == original


def test_configured_text_is_authoritative_even_when_not_customized():
    descriptor = {"skill_name": "browser-skill", "parameters": {"runtime_behavior_version": 1,
        "instructions": "Exact saved body", "isCustomized": False}}
    assert _load_authoritative(descriptor)[0] == "Exact saved body"


def test_only_empty_v1_fallback_selects_new_playbook():
    legacy = _load_authoritative({"skill_name": "browser-skill", "parameters": {}})[0]
    versioned = _load_authoritative({"skill_name": "browser-skill", "parameters": {"runtime_behavior_version": 1}})[0]
    assert legacy != versioned
    assert "snapshot` before navigation" in versioned


def test_new_skill_generation_preserves_library_bodies(monkeypatch):
    monkeypatch.setenv("NEW_AGENT_RUNTIME_V1_ENABLED", "true")
    result = new_skill_parameters({"skills_config": {"browser-skill": {"instructions": "Owner's copy", "isCustomized": False}}})
    assert result["runtime_behavior_version"] == 1
    assert result["skills_config"]["browser-skill"]["instructions"] == "Owner's copy"


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [0, 1])
async def test_reused_skill_holder_keeps_its_own_default_version(monkeypatch, version):
    from services.plugin import edge_walker
    descriptor = {"skill_name": "browser-skill", "master_skill_node_id": "holder", "parameters": {}}
    monkeypatch.setattr(edge_walker, "get_master_skill_expander", lambda: AsyncMock(return_value=[descriptor]))
    database = SimpleNamespace(get_node_parameters=AsyncMock(return_value={
        "runtime_behavior_version": version, "skill_folder": "web_agent", "skills_config": {"browser-skill": {"enabled": True}}}))
    collected = []
    await edge_walker._append_skill_entries({"type": "masterSkill"}, "holder", database, collected, "test")
    assert collected[0]["parameters"].get("runtime_behavior_version", 0) == version
    assert descriptor["parameters"] == {}
