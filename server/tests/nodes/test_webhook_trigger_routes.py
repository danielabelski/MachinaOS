"""Webhook delivery to deployed webhookTrigger nodes (RFC-0004 C3).

``deliver_webhook`` routes by the running and paused generations' snapshots:
404 for a path none declares, 405 for a method none accepts, 401 when the
header check fails. Each accepting workflow gets one event scoped to it, with
authentication headers removed and nothing broadcast.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from nodes.trigger.webhook_trigger import WebhookTriggerNode, WebhookTriggerParams
from nodes.trigger.webhook_trigger import _routes
from services import event_waiter
from services.events import dispatch as dispatch_mod
from services.workflow_sanitizer import sanitize_runtime_payload


def _control(workflow_id: str, triggers: Dict[str, Dict[str, Any]], *, disabled=()):
    nodes = [{"id": node_id, "type": "webhookTrigger", "data": {"disabled": node_id in disabled}} for node_id in triggers]
    return SimpleNamespace(workflow_id=workflow_id, graph_snapshot={"nodes": nodes, "parameters": triggers})


class _Database:
    def __init__(self, controls, saved=None):
        self.controls = controls
        self.saved = saved or {}

    async def list_active_workflow_controls(self):
        return self.controls

    async def get_node_parameters(self, node_id):
        return self.saved.get(node_id)


@pytest.fixture
def emitted(monkeypatch) -> List[Dict[str, Any]]:
    calls: List[Dict[str, Any]] = []

    async def fake_emit(event, **kwargs):
        calls.append({"event": event, **kwargs})
        return event

    monkeypatch.setattr(dispatch_mod, "emit", fake_emit)
    return calls


async def _deliver(database, *, method="POST", path="orders", headers=None):
    return await _routes.deliver_webhook(
        database, method=method, path=path, headers=headers or {}, query={}, body="{}", json_body={},
    )


async def test_a_path_no_running_workflow_declares_is_404(emitted):
    database = _Database([_control("wf-1", {"wf-1:webhookTrigger:1": {"path": "orders"}})])

    assert await _deliver(database, path="refunds") == 404
    assert await _deliver(database, path="") == 404
    assert emitted == []


async def test_a_method_the_trigger_does_not_accept_is_405(emitted):
    database = _Database([_control("wf-1", {"wf-1:webhookTrigger:1": {"path": "orders", "method": "POST"}})])

    assert await _deliver(database, method="GET") == 405
    assert emitted == []


async def test_header_authentication_is_enforced_and_fails_closed(emitted):
    trigger = {"path": "orders", "authentication": "header", "header_name": "X-API-Key"}
    database = _Database([_control("wf-1", {"t1": trigger})], saved={"t1": {**trigger, "header_value": "s3cret"}})

    assert await _deliver(database) == 401
    assert await _deliver(database, headers={"x-api-key": "wrong"}) == 401
    assert emitted == []

    unconfigured = _Database([_control("wf-1", {"t1": trigger})], saved={"t1": trigger})
    assert await _deliver(unconfigured, headers={"x-api-key": ""}) == 401


async def test_an_accepted_request_reaches_only_its_workflow_without_its_secrets(emitted):
    trigger = {"path": "orders", "authentication": "header", "header_name": "X-API-Key"}
    database = _Database(
        [_control("wf-1", {"t1": trigger}), _control("wf-2", {"t2": {"path": "other"}})],
        saved={"t1": {**trigger, "header_value": "s3cret"}},
    )

    status = await _deliver(database, headers={
        "x-api-key": "s3cret", "authorization": "Bearer x", "cookie": "session=1", "x-request-id": "r1",
    })

    assert status == 200
    assert len(emitted) == 1
    event = emitted[0]["event"]
    assert event.workflow_id == "wf-1"
    assert emitted[0]["broadcast"] is False
    assert event.data["headers"] == {"x-request-id": "r1"}


async def test_each_workflow_declaring_the_path_gets_its_own_event(emitted):
    database = _Database([
        _control("wf-1", {"t1": {"path": "orders"}}),
        _control("wf-2", {"t2": {"path": "orders", "method": "ALL"}}),
        _control("wf-3", {"t3": {"path": "orders"}}, disabled={"t3"}),
    ])

    assert await _deliver(database) == 200
    assert sorted(call["event"].workflow_id for call in emitted) == ["wf-1", "wf-2"]


async def test_a_workflow_whose_header_check_fails_is_left_out(emitted):
    guarded = {"path": "orders", "authentication": "header", "header_name": "X-API-Key"}
    database = _Database(
        [_control("wf-1", {"t1": guarded}), _control("wf-2", {"t2": {"path": "orders"}})],
        saved={"t1": {**guarded, "header_value": "s3cret"}},
    )

    assert await _deliver(database) == 200
    assert [call["event"].workflow_id for call in emitted] == ["wf-2"]


def test_a_trigger_without_a_path_matches_nothing():
    """An empty path used to match every webhook sent to the instance."""
    node_filter = WebhookTriggerNode().build_filter(WebhookTriggerParams(path=""))
    waiter_filter = event_waiter.build_webhook_filter({"path": ""})

    assert node_filter({"path": "orders"}) is False
    assert waiter_filter({"path": "orders"}) is False
    assert WebhookTriggerNode().build_filter(WebhookTriggerParams(path="orders"))({"path": "orders"}) is True


def test_start_refuses_a_webhook_without_a_path():
    graph = {"nodes": [], "edges": [], "parameters": {"t1": {"path": "  "}, "t2": {"path": "orders"}}}

    assert [error["code"] for error in WebhookTriggerNode.validate_graph("t1", graph)] == ["WEBHOOK_PATH_REQUIRED"]
    assert WebhookTriggerNode.validate_graph("t2", graph) == []
    # Parameters the caller did not supply are not judged.
    assert WebhookTriggerNode.validate_graph("t3", graph) == []


def test_the_expected_header_value_never_rides_a_snapshot():
    assert sanitize_runtime_payload({"path": "orders", "header_name": "X-API-Key", "header_value": "s3cret"}) == {
        "path": "orders", "header_name": "X-API-Key",
    }
