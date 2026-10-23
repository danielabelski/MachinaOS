"""Birth policy cannot be selected by templates, model arguments or edits."""
import pytest

from services.agent_runtime_policy import (
    AgentRuntimeParams, new_agent_parameters, preserve_runtime_version,
    runtime_behavior_version, runtime_context,
)


@pytest.mark.parametrize("value", [None, {}, {"runtime_behavior_version": 0}, {"runtime_behavior_version": True},
                                      {"runtime_behavior_version": "1"}, {"runtime_behavior_version": 2}])
def test_missing_and_untrusted_policy_is_legacy(value):
    assert runtime_behavior_version(value) == 0
    assert runtime_context(value) == {}


def test_schema_default_never_upgrades_old_nodes():
    assert AgentRuntimeParams().runtime_behavior_version == 0
    assert AgentRuntimeParams.model_json_schema()["properties"]["runtime_behavior_version"]["default"] == 0


def test_fresh_creation_gate_never_accepts_caller_policy(monkeypatch):
    monkeypatch.setenv("NEW_AGENT_RUNTIME_V1_ENABLED", "false")
    assert new_agent_parameters({"runtime_behavior_version": 1, "model": "x"}) == {"model": "x"}
    monkeypatch.setenv("NEW_AGENT_RUNTIME_V1_ENABLED", "true")
    assert new_agent_parameters({"runtime_behavior_version": 0, "model": "x"}) == {"model": "x", "runtime_behavior_version": 1}


@pytest.mark.parametrize("old", [{}, {"runtime_behavior_version": 0}, {"runtime_behavior_version": 1}])
def test_regular_edits_preserve_identity(old):
    proposed = {"runtime_behavior_version": 1 - old.get("runtime_behavior_version", 0), "model": "new"}
    saved = preserve_runtime_version(old, proposed)
    assert runtime_behavior_version(saved) == runtime_behavior_version(old)
    assert ("runtime_behavior_version" in saved) == ("runtime_behavior_version" in old)
    assert saved["model"] == "new"


def test_only_v1_payloads_gain_a_new_key():
    assert runtime_context({}) == {}
    assert runtime_context({"runtime_behavior_version": 1}) == {"runtime_behavior_version": 1}


def test_all_real_agent_roots_advertise_creation_without_schema_upgrade():
    from models.node_metadata import NODE_METADATA
    from services.node_registry import get_node_class
    from services.plugin.base import locked_tool_fields
    found = set()
    for kind in NODE_METADATA:
        cls = get_node_class(kind)
        if cls and getattr(cls, "component_kind", "") == "agent" and issubclass(cls.Params, AgentRuntimeParams):
            found.add(kind)
            assert cls.Params.model_fields["runtime_behavior_version"].default == 0
            assert NODE_METADATA[kind]["uiHints"]["creationParameters"] == {"runtime_behavior_version": 1}
            assert "runtime_behavior_version" in locked_tool_fields(cls)
    assert {"aiAgent", "chatAgent", "coding_agent", "browser_agent", "claude_code_agent", "codex_agent", "mobile_use_agent"} <= found


def test_fresh_finalizer_never_stamps_an_existing_member(monkeypatch):
    from services.agent_runtime_policy import stamp_new_graph_parameters
    monkeypatch.setenv("NEW_AGENT_RUNTIME_V1_ENABLED", "true")
    graph = {"nodes": [{"id": "old", "type": "aiAgent"}, {"id": "new", "type": "aiAgent"},
        {"id": "old-skill", "type": "masterSkill"}, {"id": "new-skill", "type": "masterSkill"}],
        "edges": [{"source": "old-skill", "target": "old", "targetHandle": "input-skill"},
                  {"source": "new-skill", "target": "new", "targetHandle": "input-skill"}]}
    parameters = {key: {} for key in ("old", "new", "old-skill", "new-skill")}
    stamp_new_graph_parameters(graph, parameters, {"new", "new-skill"})
    assert parameters["old"] == parameters["old-skill"] == {}
    assert parameters["new"]["runtime_behavior_version"] == parameters["new-skill"]["runtime_behavior_version"] == 1


@pytest.mark.parametrize("version", [0, 1])
def test_new_skill_holder_uses_existing_target_policy(monkeypatch, version):
    from services.agent_runtime_policy import prepare_new_agent_additions
    from services.graph_build import GraphAdditions, NewNode, skill_edge
    monkeypatch.setenv("NEW_AGENT_RUNTIME_V1_ENABLED", "true")
    batch = GraphAdditions(nodes=[NewNode("holder", "masterSkill", "Skills", {})],
        edges=[skill_edge("holder", "existing-agent")])
    prepared = prepare_new_agent_additions(batch, {"existing-agent": {"runtime_behavior_version": version}})
    assert prepared.nodes[0].params.get("runtime_behavior_version", 0) == version
