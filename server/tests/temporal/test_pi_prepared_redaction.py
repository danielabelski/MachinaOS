"""Pi preparation redacts configuration while preserving canonical contracts."""

from __future__ import annotations

from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.llm.protocol import ContentBlock, Message, ToolCall, message_to_wire
from services.plugin.tool import ToolBinding, ToolNode
from services.temporal.agent_activities import prepare_agent_payload, refresh_agent_tools

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_CONTEXT = {
    "node_id": "agent-1", "node_type": "coding_agent", "workflow_id": "graph-1",
    "session_id": "session-1", "generation": 1, "pi_harness_version": 1,
}
_CONFIG = {
    "api_key": "CONFIG_API_CANARY", "password": "CONFIG_PASSWORD_CANARY",
    "options": {"client_secret": "CONFIG_CLIENT_CANARY"},
    # These are configuration paths, never canonical-data exemptions.
    "provider_state": {"secret": "CONFIG_STATE_CANARY", "keep": "setting"},
    "properties": {"password": "CONFIG_PROPERTIES_CANARY", "keep": "setting"},
}
_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "password": {"type": "string"}, "api_key": {"type": "string"},
        "provider_state": {
            "type": "object", "properties": {"secret": {"type": "string"}},
            "required": ["secret"],
        },
    },
    "required": ["password", "api_key"],
}
_DEFINITION = {"name": "configure_login", "description": "Configure a login", "parameters": _SCHEMA}
_IMAGE = {
    "kind": "image", "path": "uploads/login.png", "workflow_id": "graph-1",
    "filename": "login.png", "mime_type": "image/png", "size_bytes": 42,
    "sha256": "a" * 64,
}


class _CredentialArgumentTool(ToolNode, abstract=True):
    @classmethod
    async def tool_bindings(cls, parameters):
        return [ToolBinding(
            key="login", name=_DEFINITION["name"], description=_DEFINITION["description"],
            schema=deepcopy(_SCHEMA), parameters={"authorization": "BINDING_AUTH_CANARY"},
        )]


@pytest.fixture
def prepared(monkeypatch):
    raw = '{ "password": "canonical-password", "api_key": "canonical-argument", "provider_state": {"secret": "argument-state"} }'
    call = ToolCall.from_raw(id="call-1", name="configure_login", arguments=raw)
    conversation = [
        dict(message_to_wire(Message(role="user", content="Inspect this login", blocks=[
            ContentBlock(type="text", text="Inspect this login"),
            ContentBlock(type="image", source={"kind": "file_ref", "ref": _IMAGE},
                         metadata={"password": "canonical-media-metadata"}),
        ]))),
        dict(message_to_wire(Message(role="assistant", tool_calls=[call], blocks=[
            ContentBlock(type="reasoning", text="Inspect", metadata={"secret": "reasoning-signature"}),
            ContentBlock(type="tool_call", tool_call=call),
        ], provider_state={"secret": "opaque-provider-continuation", "nested": {"api_key": "continuation-signature"}}))),
        dict(message_to_wire(Message(role="tool", content="Recorded login result", tool_call_id="call-1", name="configure_login"))),
    ]
    node_parameters = {"provider": "openai", "model": "test-model", "prompt": "Continue", **deepcopy(_CONFIG)}
    snapshot = {"agent-1": node_parameters, "tool-1": deepcopy(_CONFIG)}
    tool_info = {"node_id": "tool-1", "node_type": "credentialFixture", "parameters": deepcopy(_CONFIG), "label": "Login"}
    graph = {
        "nodes": [{"id": "tool-1", "type": "credentialFixture", "data": deepcopy(_CONFIG)}],
        "edges": [{"source": "tool-1", "target": "agent-1", "targetHandle": "input-tools"}],
    }
    database = SimpleNamespace(
        get_node_parameters=AsyncMock(side_effect=lambda identifier: deepcopy(snapshot.get(identifier, {}))),
        get_user_settings=AsyncMock(return_value=None), get_workflow=AsyncMock(return_value=SimpleNamespace(data=graph)),
        get_workflow_control_generation=AsyncMock(return_value=None),
    )
    monkeypatch.setattr("core.container.container", SimpleNamespace(
        database=lambda: database, auth_service=lambda: SimpleNamespace(),
        ai_service=lambda: SimpleNamespace(), workflow_service=lambda: SimpleNamespace(),
    ))
    monkeypatch.setattr("core.config.Settings", lambda: SimpleNamespace(
        max_concurrent_subagents=3, max_delegation_depth=2,
        agent_recursion_limit=200, tool_result_max_chars=100_000,
    ))
    monkeypatch.setattr("services.employees.permissions.assert_runtime_access", AsyncMock())
    monkeypatch.setattr("services.llm.config.is_model_valid_for_provider", lambda *_a: True)
    monkeypatch.setattr("services.llm.config.resolve_max_tokens", lambda *_a: 1000)
    monkeypatch.setattr("services.llm.config.resolve_temperature", lambda *_a: 0.2)
    monkeypatch.setattr("services.compaction.get_compaction_service", lambda: None)
    monkeypatch.setattr("services.node_registry.get_node_class", lambda name: _CredentialArgumentTool if name == "credentialFixture" else None)

    async def connections(*_args, **_kwargs):
        return {"kind": "context", "node_id": "context-1"}, [], [deepcopy(tool_info)], {}, None

    monkeypatch.setattr("services.plugin.edge_walker.collect_agent_connections", connections)
    monkeypatch.setattr("services.agent_context.load_conversation", AsyncMock(return_value=conversation))
    monkeypatch.setattr("services.temporal.agent_activities._chat_run_options", AsyncMock(return_value={}))
    monkeypatch.setattr("services.chat.stream.chat_stream_for", lambda _context: {"run_id": "chat-1"})
    monkeypatch.setattr("services.chat.guide.chat_turn", AsyncMock(return_value=("Canonical system", "Continue")))
    monkeypatch.setattr("services.chat.guide.run_attachments", AsyncMock(return_value=[deepcopy(_IMAGE)]))
    return SimpleNamespace(snapshot=snapshot, conversation=conversation, database=database, graph=graph, tool_info=tool_info)


def _assert_configuration_redacted(value):
    serialized = json.dumps(value)
    assert "CONFIG_" not in serialized
    assert "BINDING_AUTH_CANARY" not in serialized


async def test_prepare_preserves_exact_wires_schema_and_user_media(prepared):
    original = deepcopy(prepared.conversation)
    payload = await prepare_agent_payload({**_CONTEXT, "parameter_snapshot": prepared.snapshot})

    _assert_configuration_redacted(payload)
    assert payload["conversation"] == original
    assert json.dumps(payload["conversation"]) == json.dumps(original)
    assert payload["user_images"] == [_IMAGE]
    assert payload["tools"][0]["definition"] == _DEFINITION
    assert payload["tools"][0]["parameters"]["provider_state"] == {"keep": "setting"}
    assert payload["tools"][0]["tool_info"]["parameters"]["properties"] == {"keep": "setting"}
    assert prepared.snapshot["agent-1"]["api_key"] == "CONFIG_API_CANARY"
    assert prepared.conversation == original


@pytest.mark.parametrize("browser_runtime_version", [0, 1])
async def test_refresh_preserves_schema_and_redacts_graph_and_parameter_updates(prepared, browser_runtime_version):
    result = await refresh_agent_tools({
        "pi_harness_version": 1, "browser_runtime_version": browser_runtime_version,
        "invoking_agent_node_id": "agent-1", "agent_node_type": "coding_agent", "workflow_id": "graph-1",
        "parameter_snapshot": {"agent-1": {}},
        "operations": [{"type": "add_node", "node_type": "credentialFixture", "minted_id": "tool-1", "parameters": deepcopy(_CONFIG)}],
    })

    _assert_configuration_redacted(result)
    assert result["tools"][0]["definition"] == _DEFINITION
    assert result["tools"][0]["parameters"]["properties"] == {"keep": "setting"}
    assert result["tools"][0]["tool_info"]["parameters"]["provider_state"] == {"keep": "setting"}
    assert result["parameter_updates"]["tool-1"]["properties"] == {"keep": "setting"}
    assert result["graph_snapshot"]["nodes"][0]["data"]["provider_state"] == {"keep": "setting"}
    assert prepared.graph["nodes"][0]["data"]["password"] == "CONFIG_PASSWORD_CANARY"


async def test_unmarked_native_prepare_retains_legacy_configuration_behavior(prepared):
    context = {**_CONTEXT, "node_type": "aiAgent", "parameter_snapshot": prepared.snapshot}
    context.pop("pi_harness_version")
    payload = await prepare_agent_payload(context)

    assert payload["conversation"] == prepared.conversation
    assert payload["tools"][0]["definition"] == _DEFINITION
    assert payload["parameter_snapshot"]["agent-1"]["api_key"] == "CONFIG_API_CANARY"
    assert payload["tools"][0]["parameters"]["authorization"] == "BINDING_AUTH_CANARY"
