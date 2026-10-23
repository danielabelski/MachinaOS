"""Warnings use their own projection and never mutate node execution state."""
import json
from unittest.mock import AsyncMock

import pytest
from starlette.websockets import WebSocketState

from services.status_broadcaster import StatusBroadcaster


def wait(**changes):
    return {"workflow_id": "wf", "node_id": "agent", "execution_key": "exec", "request_id": "request",
        "execution_id": "run", "root_execution_id": "root", "runtime_behavior_version": 1,
        "seq": 1, "state": "waiting", "stage": "sdk_request", "elapsed_ms": 5000,
        "scope": "generation", "generation": 2, **changes}


class Socket:
    client_state = application_state = WebSocketState.CONNECTED
    def __init__(self):
        self.frames = []
    async def send_text(self, text):
        self.frames.append(json.loads(text))


@pytest.mark.asyncio
async def test_warning_wire_shape_snapshot_and_stale_rejection(monkeypatch):
    monkeypatch.setattr("services.chat.relay.active_relay", lambda: None)
    broadcaster, socket = StatusBroadcaster(), Socket()
    broadcaster._connections.add(socket)
    before = dict(broadcaster.get_status()["nodes"])
    assert await broadcaster.publish_llm_wait(wait())
    assert socket.frames[-1] == {"type": "node_status", "workflow_id": "wf", "node_id": "agent", "data": {"llm_wait": wait()}}
    assert broadcaster.get_status()["nodes"] == before
    assert broadcaster.get_status()["llm_waits"] == [wait()]
    assert await broadcaster.publish_llm_wait(wait(seq=2, state="cleared"))
    assert not await broadcaster.publish_llm_wait(wait(seq=3))
    assert broadcaster.get_status()["llm_waits"][0]["state"] == "cleared"


@pytest.mark.asyncio
async def test_paused_deployment_keeps_direct_workspace_task_warning(monkeypatch):
    monkeypatch.setattr("services.chat.relay.active_relay", lambda: None)
    broadcaster = StatusBroadcaster()
    assert await broadcaster.publish_llm_wait(wait())
    direct = wait(scope="workspace", workspace_epoch=4, invocation_id="direct", request_id="direct", execution_key="direct")
    assert await broadcaster.publish_llm_wait(direct)
    await broadcaster.broadcast({"type": "workflow_control_status", "workflow_id": "wf",
        "data": {"state": "paused", "generation": 2, "root_execution_id": "root"}})
    active = [value for value in broadcaster.get_status()["llm_waits"] if value["state"] == "waiting"]
    assert active == [direct]
    assert not await broadcaster.publish_llm_wait(wait(request_id="late"))


@pytest.mark.asyncio
async def test_worker_uses_independent_relay_hook(monkeypatch):
    class Relay:
        def __init__(self):
            self.items = []
        def offer_llm_wait(self, item):
            self.items.append(item)
    relay = Relay()
    monkeypatch.setattr("services.chat.relay.active_relay", lambda: relay)
    broadcaster = StatusBroadcaster()
    broadcaster.broadcast = AsyncMock()
    assert await broadcaster.publish_llm_wait(wait())
    assert relay.items == [wait()]
    broadcaster.broadcast.assert_not_awaited()
    assert "llm_waits" not in broadcaster.get_status()
