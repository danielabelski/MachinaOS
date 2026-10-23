"""A standalone worker's chat events reach the backend (services/chat/relay.py).

On the worker: events leave in the order they were made, a full queue or a
lost frame costs only those events and ends in a resync for their sessions,
and close() sends what is queued. On the backend: ``chat_run_publish`` takes
the worker socket alone, publishes run events into the hub (which numbers and
de-duplicates them), sends only the two allowed broadcasts, and refuses
anything malformed without losing the rest. Publishers route through the
relay only while one is active.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any, Dict, List, Optional

import pytest

import services.chat.relay as relay_module
from services.chat.hub import publish_run_event
from services.chat.relay import ChatRelay, handle_chat_run_publish
from tests.services.chat._helpers import FakeSocket


async def settle(times: int = 10) -> None:
    for _ in range(times):
        await asyncio.sleep(0)


async def until(condition, tries: int = 200) -> None:
    for _ in range(tries):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("the relay never got there")


class FakeConnection:
    """Records the frames the relay sends; ``fail`` makes sends raise."""

    def __init__(self, *, fail: bool = False) -> None:
        self.frames: List[Dict[str, Any]] = []
        self.fail = fail
        self._closed = asyncio.Event()

    async def send_str(self, data: str) -> None:
        if self.fail:
            raise ConnectionError("the connection dropped")
        self.frames.append(json.loads(data))

    def __aiter__(self):
        async def messages():
            await self._closed.wait()
            if False:  # pragma: no cover - makes this an async generator
                yield None

        return messages()

    async def close(self) -> None:
        self._closed.set()

    def items(self) -> List[Dict[str, Any]]:
        return [item for frame in self.frames for item in frame["items"]]


def connector(*connections: FakeConnection, gate: Optional[asyncio.Event] = None):
    pending = list(connections)

    async def connect():
        if gate is not None:
            await gate.wait()
        return pending.pop(0)

    return connect


def offer(relay: ChatRelay, suffix: str, *, session_id: str = "wf", run_id: str = "r_1", **fields: Any) -> None:
    event_key = fields.pop("event_key", None)
    relay.offer_run_event(
        run_id=run_id,
        session_id=session_id,
        workflow_id=session_id,
        suffix=suffix,
        fields=fields,
        event_key=event_key,
    )


# ---- the worker's side ------------------------------------------------------


async def test_events_leave_in_order():
    connection = FakeConnection()
    relay = ChatRelay(connector(connection), queue_size=16, batch_size=2)
    relay.start()
    offer(relay, "started", kind="message")
    offer(relay, "text.content", message_id="m", delta="Hel")
    offer(relay, "text.content", message_id="m", delta="lo")
    relay.offer_broadcast("chat.updated", {"type": "com.opencompany.chat.updated"}, session_id="wf")
    await settle()
    await relay.close()
    items = connection.items()
    assert [item.get("suffix") or item["kind"] for item in items] == ["started", "text.content", "text.content", "broadcast"]
    assert [item["fields"].get("delta") for item in items[1:3]] == ["Hel", "lo"]
    assert all(len(frame["items"]) <= 2 and frame["type"] == "chat_run_publish" for frame in connection.frames)
    assert relay.dropped == 0


async def test_a_full_queue_drops_and_then_resyncs_the_session():
    connection = FakeConnection()
    gate = asyncio.Event()
    relay = ChatRelay(connector(connection, gate=gate), queue_size=2, batch_size=10)
    relay.start()
    offer(relay, "started")
    offer(relay, "step.started", step_id="s1", step_name="Checking")
    offer(relay, "step.finished", step_id="s1", state="done")
    assert relay.dropped == 1
    gate.set()
    await settle()
    await relay.close()
    items = connection.items()
    assert [item.get("suffix") for item in items[:2]] == ["started", "step.started"]
    # The hole comes last: the subscribers re-snapshot after what did arrive.
    assert items[-1] == {"kind": "resync", "session_ids": ["wf"]}


async def test_a_lost_frame_is_not_resent_but_resyncs_after_reconnecting():
    broken, healthy = FakeConnection(fail=True), FakeConnection()
    relay = ChatRelay(connector(broken, healthy), queue_size=16, batch_size=10, retry_delays=(0,))
    relay.start()
    offer(relay, "started", session_id="wf")
    # The resync goes out as soon as a connection is back.
    await until(lambda: healthy.frames)
    assert relay.dropped == 1
    offer(relay, "step.started", session_id="wf", step_id="s1", step_name="Checking")
    await relay.close()
    assert healthy.items() == [
        {"kind": "resync", "session_ids": ["wf"]},
        {
            "kind": "run_event",
            "run_id": "r_1",
            "session_id": "wf",
            "workflow_id": "wf",
            "suffix": "step.started",
            "fields": {"step_id": "s1", "step_name": "Checking"},
            "event_key": None,
        },
    ]


async def test_close_sends_what_is_queued():
    connection = FakeConnection()
    relay = ChatRelay(connector(connection), queue_size=16, batch_size=10)
    relay.start()
    offer(relay, "started")
    offer(relay, "finished", outcome={"type": "success"})
    await relay.close()
    assert [item["suffix"] for item in connection.items()] == ["started", "finished"]


async def test_a_backend_that_never_answers_costs_only_the_queued_events():
    async def unreachable():
        raise OSError("connection refused")

    relay = ChatRelay(unreachable, queue_size=16, batch_size=10, retry_delays=(0,))
    relay.start()
    offer(relay, "started")
    await settle()
    await relay.close(timeout=0.5)
    assert relay.dropped == 1


def llm_wait(**changes):
    return {"workflow_id": "wf", "node_id": "agent", "execution_key": "execution", "request_id": "request",
        "execution_id": "run", "root_execution_id": "root", "runtime_behavior_version": 1,
        "seq": 1, "state": "waiting", "stage": "sdk_request", "elapsed_ms": 5000,
        "scope": "generation", "generation": 2, **changes}


async def test_diagnostic_clear_survives_chat_queue_overflow():
    connection = FakeConnection()
    relay = ChatRelay(connector(connection), queue_size=1, batch_size=10)
    relay.start()
    relay.offer_llm_wait(llm_wait())
    await until(lambda: connection.frames)
    offer(relay, "started")
    offer(relay, "step.started")
    relay.offer_llm_wait(llm_wait(seq=2, state="cleared"))
    assert relay.dropped == 1
    await relay.close()
    diagnostics = [item["data"] for item in connection.items() if item["kind"] == "llm_wait"]
    assert [(item["seq"], item["state"]) for item in diagnostics] == [(1, "waiting"), (2, "cleared")]


async def test_failed_diagnostic_clear_is_reconciled_after_reconnect():
    class FailClear(FakeConnection):
        async def send_str(self, data):
            if any(item.get("data", {}).get("state") == "cleared" for item in json.loads(data)["items"]):
                raise ConnectionError("lost the clear")
            await super().send_str(data)
    broken, healthy = FailClear(), FakeConnection()
    relay = ChatRelay(connector(broken, healthy), queue_size=1, batch_size=10, retry_delays=(0,))
    relay.start()
    relay.offer_llm_wait(llm_wait())
    await until(lambda: broken.frames)
    relay.offer_llm_wait(llm_wait(seq=2, state="cleared"))
    relay.offer_llm_wait(llm_wait(request_id="concurrent", execution_key="other"))
    await until(lambda: healthy.frames)
    await relay.close()
    assert {item["data"]["request_id"]: item["data"]["state"] for item in healthy.items()} == {
        "request": "cleared", "concurrent": "waiting"}
    assert relay.dropped == 0


async def test_reconnect_snapshots_sent_states_and_terminal_tombstones():
    first, second = FakeConnection(), FakeConnection()
    relay = ChatRelay(connector(first, second), queue_size=1, batch_size=10, retry_delays=(0,))
    relay.start()
    relay.offer_llm_wait(llm_wait())
    relay.offer_llm_wait(llm_wait(request_id="concurrent", execution_key="other"))
    await until(lambda: first.frames)
    relay.offer_llm_wait(llm_wait(seq=2, state="cleared"))
    await until(lambda: any(item["data"]["state"] == "cleared" for item in first.items()))
    await first.close()
    await until(lambda: second.frames)
    relay.offer_llm_wait(llm_wait(seq=100))
    await relay.close()
    assert {item["data"]["request_id"]: (item["data"]["seq"], item["data"]["state"])
            for item in second.items()} == {"request": (2, "cleared"), "concurrent": (1, "waiting")}


async def test_clear_during_send_remains_dirty_until_its_own_send():
    entered, release = asyncio.Event(), asyncio.Event()
    class InFlight(FakeConnection):
        async def send_str(self, data):
            if not self.frames:
                entered.set()
                await release.wait()
            await super().send_str(data)
    connection = InFlight()
    relay = ChatRelay(connector(connection), queue_size=1, batch_size=10)
    relay.start()
    relay.offer_llm_wait(llm_wait())
    await entered.wait()
    relay.offer_llm_wait(llm_wait(seq=2, state="cleared"))
    release.set()
    await relay.close()
    assert [(item["data"]["seq"], item["data"]["state"]) for item in connection.items()] == [
        (1, "waiting"), (2, "cleared")]


def test_diagnostic_capacity_preserves_unsent_clears_and_existing_requests():
    cache = relay_module._LLMWaitCache(limit=2)
    assert cache.offer(llm_wait())
    assert cache.offer(llm_wait(request_id="second"))
    cache.sent(cache.batch())
    assert cache.offer(llm_wait(seq=2, state="cleared"))
    assert not cache.offer(llm_wait(request_id="third"))
    assert cache.offer(llm_wait(request_id="second", seq=2, state="cleared"))
    cache.sent(cache.batch()[:1])
    assert cache.offer(llm_wait(request_id="third"))
    assert len(cache.records) == 2
    assert not cache.offer(llm_wait(request_id="second", seq=100))
    assert {item["request_id"] for _, item in cache.batch()} == {"second", "third"}


def test_diagnostic_cache_filters_private_fields_invalid_inputs_and_expired_records():
    now = [0]
    cache = relay_module._LLMWaitCache(limit=1, ttl=10, clock=lambda: now[0])
    assert not cache.offer(llm_wait(seq=True))
    assert cache.offer(llm_wait(prompt="private", api_key="secret", headers={"Authorization": "secret"}))
    _, item = cache.batch()[0]
    assert not {"prompt", "api_key", "headers"}.intersection(item)
    assert not cache.offer(llm_wait(seq=1, state="cleared"))
    now[0] = 11
    assert not cache.pending() and not cache.records
    assert cache.offer(llm_wait(request_id="new"))


# ---- routing ------------------------------------------------------------------


class RecordingRelay:
    def __init__(self) -> None:
        self.run_events: List[Dict[str, Any]] = []
        self.broadcasts: List[tuple] = []

    def offer_run_event(self, **event: Any) -> None:
        self.run_events.append(event)

    def offer_broadcast(self, wire_key: str, data: Dict[str, Any], *, session_id: Optional[str] = None) -> None:
        self.broadcasts.append((wire_key, data, session_id))


async def test_run_events_go_to_the_local_hub_unless_a_relay_is_active(monkeypatch, hub):
    socket = FakeSocket()
    hub.subscribe(socket, "wf")
    assert publish_run_event(run_id="r_1", session_id="wf", workflow_id="wf", suffix="started") is not None
    recording = RecordingRelay()
    monkeypatch.setattr(relay_module, "_active", recording)
    assert publish_run_event(run_id="r_1", session_id="wf", workflow_id="wf", suffix="step.started", fields={"step_id": "s"}) is None
    await settle()
    assert [event["data"]["seq"] for event in socket.events()] == [1]
    assert recording.run_events == [
        {"run_id": "r_1", "session_id": "wf", "workflow_id": "wf", "suffix": "step.started", "fields": {"step_id": "s"}, "event_key": None}
    ]


async def test_chat_and_approval_broadcasts_go_through_an_active_relay(monkeypatch, frames):
    from services.approvals.events import broadcast_approval_change
    from services.approvals.listeners import ApprovalChange
    from services.chat_thread import announce_chat_updated

    recording = RecordingRelay()
    monkeypatch.setattr(relay_module, "_active", recording)
    await announce_chat_updated("wf", "assistant")
    await broadcast_approval_change(ApprovalChange(stage="sent", approval_id="a_1", workflow_id="wf", status="sent", revision=3))
    assert frames == []
    (chat_key, chat_data, chat_session), (approval_key, approval_data, approval_session) = recording.broadcasts
    assert (chat_key, chat_data["type"], chat_data["data"]["session_id"], chat_session) == (
        "chat.updated",
        "com.opencompany.chat.updated",
        "wf",
        "wf",
    )
    assert (approval_key, approval_data["type"], approval_session) == ("approval_lifecycle", "com.opencompany.approval.sent", "wf")


def test_only_the_standalone_worker_starts_the_relay():
    from pathlib import Path

    from services.temporal.worker import run_standalone_worker

    source = inspect.getsource(run_standalone_worker)
    assert "start_relay()" in source and "await stop_relay()" in source
    main = (Path(__file__).resolve().parents[3] / "main.py").read_text(encoding="utf-8")
    assert "start_relay" not in main


# ---- the backend's side -------------------------------------------------------


def worker_socket() -> FakeSocket:
    return FakeSocket(path="/ws/internal")


def run_event(suffix: str, **extra: Any) -> Dict[str, Any]:
    item = {"kind": "run_event", "run_id": "r_1", "session_id": "wf", "workflow_id": "wf", "suffix": suffix, "fields": {}, "event_key": None}
    item.update(extra)
    return item


async def test_a_client_socket_is_refused(hub):
    subscriber = FakeSocket()
    hub.subscribe(subscriber, "wf")
    reply = await handle_chat_run_publish({"type": "chat_run_publish", "items": [run_event("started")]}, FakeSocket())
    await settle()
    assert reply == {"success": False, "error": "access_denied"}
    assert subscriber.sent == []


async def test_the_worker_socket_publishes_into_the_hub(hub):
    subscriber = FakeSocket()
    hub.subscribe(subscriber, "wf")
    reply = await handle_chat_run_publish(
        {
            "items": [
                run_event("started", fields={"kind": "message"}, event_key="started"),
                run_event("started", fields={"kind": "message"}, event_key="started"),
                run_event("step.started", fields={"step_id": "s1", "step_name": "Checking"}),
            ]
        },
        worker_socket(),
    )
    await settle()
    assert reply == {"success": True, "published": 3, "refused": 0}
    events = subscriber.events()
    # The hub numbers them and drops the repeated key, as for its own.
    assert [(event["type"], event["data"]["seq"]) for event in events] == [
        ("com.opencompany.chat.run.started", 1),
        ("com.opencompany.chat.run.step.started", 2),
    ]
    assert events[0]["data"]["hub_epoch"] == hub.epoch


async def test_malformed_items_are_refused_without_losing_the_rest(hub, frames):
    subscriber = FakeSocket()
    hub.subscribe(subscriber, "wf")
    reply = await handle_chat_run_publish(
        {
            "items": [
                run_event("not.a.suffix"),
                run_event("started", run_id=""),
                run_event("started", fields=["not", "a", "dict"]),
                {"kind": "broadcast", "type": "node_status", "data": {"type": "com.opencompany.chat.updated"}},
                {"kind": "broadcast", "type": "chat.updated", "data": {"type": "com.opencompany.approval.sent"}},
                {"kind": "mystery"},
                "not an item",
                run_event("started"),
            ]
        },
        worker_socket(),
    )
    await settle()
    assert reply == {"success": True, "published": 1, "refused": 7}
    assert [event["data"]["seq"] for event in subscriber.events()] == [1]
    assert frames == []


async def test_too_many_items_are_refused(hub):
    reply = await handle_chat_run_publish({"items": [run_event("started")] * (relay_module.MAX_ITEMS + 1)}, worker_socket())
    assert reply == {"success": False, "error": "invalid_items"}


async def test_the_two_relayed_broadcasts_reach_every_socket(hub, frames):
    chat = {"specversion": "1.0", "type": "com.opencompany.chat.updated", "data": {"session_id": "wf"}}
    approval = {"specversion": "1.0", "type": "com.opencompany.approval.sent", "data": {"approval_id": "a_1"}}
    reply = await handle_chat_run_publish(
        {
            "items": [
                {"kind": "broadcast", "type": "chat.updated", "data": chat},
                {"kind": "broadcast", "type": "approval_lifecycle", "data": approval},
            ]
        },
        worker_socket(),
    )
    assert reply["published"] == 2
    assert frames == [{"type": "chat.updated", "data": chat}, {"type": "approval_lifecycle", "data": approval}]


async def test_a_resync_tells_the_sessions_subscribers(hub):
    subscriber, other = FakeSocket(), FakeSocket()
    hub.subscribe(subscriber, "wf")
    hub.subscribe(other, "elsewhere")
    await handle_chat_run_publish({"items": [{"kind": "resync", "session_ids": ["wf"]}]}, worker_socket())
    await settle()
    (event,) = subscriber.events()
    assert event["data"]["name"] == "opencompany.resync"
    assert other.sent == []


# ---- end to end ---------------------------------------------------------------


class Loopback(FakeConnection):
    """Hands each frame to the backend's handler, as /ws/internal would."""

    def __init__(self) -> None:
        super().__init__()
        self.replies: List[Dict[str, Any]] = []
        self._socket = worker_socket()

    async def send_str(self, data: str) -> None:
        self.replies.append(await handle_chat_run_publish(json.loads(data), self._socket))


async def test_reconnected_diagnostic_snapshot_clears_the_backend_warning(monkeypatch):
    from services.status_broadcaster import StatusBroadcaster
    broadcaster = StatusBroadcaster()
    monkeypatch.setattr("services.status_broadcaster.get_status_broadcaster", lambda: broadcaster)
    class LostClear(Loopback):
        async def send_str(self, data):
            if any(item.get("data", {}).get("state") == "cleared" for item in json.loads(data)["items"]):
                raise ConnectionError("clear lost before backend received it")
            await super().send_str(data)
    broken, recovered = LostClear(), Loopback()
    relay = ChatRelay(connector(broken, recovered), queue_size=1, batch_size=10, retry_delays=(0,))
    relay.start()
    relay.offer_llm_wait(llm_wait())
    await until(lambda: broken.replies)
    assert broadcaster.get_status()["llm_waits"][0]["state"] == "waiting"
    relay.offer_llm_wait(llm_wait(seq=2, state="cleared"))
    await until(lambda: recovered.replies)
    await relay.close()
    assert broadcaster.get_status()["llm_waits"][0]["state"] == "cleared"
    assert all(reply["refused"] == 0 for reply in recovered.replies)


async def test_a_worker_run_reaches_a_backend_subscriber_in_order(hub):
    subscriber = FakeSocket()
    hub.subscribe(subscriber, "wf")
    loopback = Loopback()
    relay = ChatRelay(connector(loopback), queue_size=64, batch_size=3)
    relay.start()
    offer(relay, "started", kind="message")
    offer(relay, "text.started", message_id="r_1.0.0")
    for index, delta in enumerate(["Saturday ", "works", "."]):
        offer(relay, "text.content", message_id="r_1.0.0", delta=delta, event_key=f"text:{index}")
    offer(relay, "text.ended", message_id="r_1.0.0", final=True)
    offer(relay, "finished", outcome={"type": "success"}, result={"no_reply": False})
    await relay.close()
    await settle()
    events = subscriber.events()
    assert [event["data"]["seq"] for event in events] == [1, 2, 3, 4, 5, 6, 7]
    assert "".join(event["data"].get("delta", "") for event in events) == "Saturday works."
    assert events[-1]["type"] == "com.opencompany.chat.run.finished"
    assert all(reply["success"] and reply["refused"] == 0 for reply in loopback.replies)


@pytest.fixture(autouse=True)
def _no_relay_left_behind(monkeypatch):
    monkeypatch.setattr(relay_module, "_active", None)
    yield
