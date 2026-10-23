"""Chat events from a worker in another process.

The sockets that follow a chat session are in the backend, and so is the hub
that writes to them (``services/chat/hub.py``). An activity on a standalone
worker (``python -m services.temporal.worker``) runs where no socket is, so
there the run events it publishes, and the identity-only ``chat.updated`` and
``approval_lifecycle`` broadcasts it makes, travel to the backend over the
worker's own ``/ws/internal`` connection as ``chat_run_publish`` frames, in
the order they were made. The backend publishes each run event into its hub,
which numbers and de-duplicates it as it would its own, and sends each
broadcast to its sockets.

- **Only on a standalone worker.** ``run_standalone_worker`` starts the
  relay; the backend and its embedded workers publish straight into the hub.
- **In order.** One queue and one writer per process.
- **Bounded, never blocking.** Past ``hub.relay_queue_size``
  (``config/chat_defaults.json``) an event is dropped, and so is a frame the
  connection lost. The next frame that gets through ends with a resync for
  the sessions those belonged to, so their subscribers take a fresh snapshot
  instead of reading a stream with a hole in it. An event is a courtesy: the
  rows are the record.
- **Model wait diagnostics.** A separate bounded latest-state cache keeps
  clear tombstones through failed sends and replays its snapshot on reconnect.
  It never consumes chat queue capacity or changes provider calls.
- **Internal only.** The backend takes ``chat_run_publish`` from
  ``/ws/internal`` alone (the worker token, ``services/authz``) and refuses
  it on a client's socket.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict, deque
from contextlib import suppress
from typing import Any, AsyncIterator, Awaitable, Callable, Deque, Dict, List, Mapping, Optional, Protocol, Set

import orjson
from pydantic_core import to_jsonable_python

from core.logging import get_logger
from services.chat.config import hub_setting
from services.chat.events import RUN_SUFFIXES
from services.ws_handler_registry import ws_handler

logger = get_logger(__name__)

#: WebSocket ``type`` of a relay frame.
WIRE_TYPE = "chat_run_publish"
#: Broadcasts a worker may hand the backend to send, and the CloudEvent type
#: (prefix) each must carry. Both are identity only: clients refetch through
#: authorized handlers.
RELAYED_BROADCASTS: Mapping[str, str] = {
    "chat.updated": "com.opencompany.chat.updated",
    "approval_lifecycle": "com.opencompany.approval.",
}
#: Most items one frame may carry.
MAX_ITEMS = 500
_MAX_ID = 256
_RETRY_DELAYS_S = (0.5, 1.0, 2.0, 5.0, 10.0)
_WAIT_CACHE_LIMIT = 4096
_WAIT_CACHE_TTL = 3600.0


class RelayConnection(Protocol):
    """What the relay needs of a connection to the backend."""

    async def send_str(self, data: str) -> None: ...

    def __aiter__(self) -> AsyncIterator[Any]: ...

    async def close(self) -> None: ...


Connect = Callable[[], Awaitable[RelayConnection]]


def _dumps(value: Any) -> str:
    return orjson.dumps(value, default=to_jsonable_python).decode()


class _LLMWaitCache:
    """Latest safe state per request, independent of the lossy chat queue.

    A clear is a terminal tombstone. Retain sent states for reconnects;
    never evict an unsent clear to admit another request.
    """

    def __init__(self, *, limit=_WAIT_CACHE_LIMIT, ttl=_WAIT_CACHE_TTL, clock=time.monotonic):
        self.limit, self.ttl, self.clock = limit, ttl, clock
        self.records = OrderedDict()
        self.dirty = set()

    def _prune(self):
        now = self.clock()
        for key, (updated, _) in list(self.records.items()):
            if now - updated > self.ttl:
                self.records.pop(key, None)
                self.dirty.discard(key)

    def offer(self, raw):
        from services.llm_wait import validate_wait
        item = validate_wait(raw)
        if item is None:
            return False
        self._prune()
        key = tuple(item[field] for field in ("workflow_id", "node_id", "execution_key", "request_id"))
        previous = self.records.get(key)
        if previous and (previous[1]["seq"] >= item["seq"] or previous[1]["state"] == "cleared"):
            return False
        if previous is None and len(self.records) >= self.limit:
            evict = next((candidate for candidate, (_, value) in self.records.items()
                          if value["state"] == "cleared" and candidate not in self.dirty), None)
            if evict is None:
                # Existing requests can still clear at capacity. A refused
                # new request has never published a wait that needs clearing.
                return False
            self.records.pop(evict)
        self.records[key] = (self.clock(), item)
        self.records.move_to_end(key)
        self.dirty.add(key)
        return True

    def reconnect(self):
        self._prune()
        self.dirty.update(self.records)

    def batch(self, limit=MAX_ITEMS):
        self._prune()
        result = []
        for key, (_, value) in self.records.items():
            if key in self.dirty:
                result.append((key, value))
                if len(result) >= limit:
                    break
        return result

    def sent(self, batch):
        for key, item in batch:
            current = self.records.get(key)
            if current is not None and current[1]["seq"] == item["seq"]:
                self.dirty.discard(key)

    def pending(self):
        self._prune()
        return bool(self.dirty)


class ChatRelay:
    """Sends this process's chat events to the backend, in order."""

    def __init__(
        self,
        connect: Connect,
        *,
        queue_size: int,
        batch_size: int,
        retry_delays: tuple = _RETRY_DELAYS_S,
    ) -> None:
        self._connect = connect
        self._queue_size = max(1, int(queue_size))
        self._batch_size = max(1, min(int(batch_size), MAX_ITEMS - 1))
        self._retry_delays = retry_delays
        self._queue: Deque[Dict[str, Any]] = deque()
        self._llm_waits = _LLMWaitCache()
        #: Sessions that lost an event since their last resync went out.
        self._lost: Set[str] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._wake: Optional[asyncio.Event] = None
        self._task: Optional[asyncio.Task] = None
        self._closing = False
        self._overflowing = False
        #: Events dropped so far (a full queue, or a frame the connection lost).
        self.dropped = 0

    # ---- producing ---------------------------------------------------

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self._task = self._loop.create_task(self._run(), name="chat-relay")

    def offer_run_event(
        self,
        *,
        run_id: str,
        session_id: str,
        workflow_id: Optional[str],
        suffix: str,
        fields: Optional[Mapping[str, Any]] = None,
        event_key: Optional[str] = None,
    ) -> None:
        self._offer(
            {
                "kind": "run_event",
                "run_id": run_id,
                "session_id": session_id,
                "workflow_id": workflow_id,
                "suffix": suffix,
                "fields": dict(fields or {}),
                "event_key": event_key,
            }
        )

    def offer_broadcast(self, wire_key: str, data: Mapping[str, Any], *, session_id: Optional[str] = None) -> None:
        self._offer({"kind": "broadcast", "type": wire_key, "data": dict(data), "session_id": session_id})

    def offer_llm_wait(self, delta: Mapping[str, Any]) -> None:
        from services.llm_wait import validate_wait
        item = validate_wait(dict(delta))
        if item is None or self._loop is None or self._closing:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            self._offer_llm_wait(item)
        else:
            self._loop.call_soon_threadsafe(self._offer_llm_wait, item)

    def _offer_llm_wait(self, item):
        if not self._closing and self._llm_waits.offer(item):
            self._notify()

    def _offer(self, item: Dict[str, Any]) -> None:
        if self._loop is None or self._closing:
            self._drop([item])
            return
        if len(self._queue) >= self._queue_size:
            if not self._overflowing:
                logger.warning("Chat relay queue is full; dropping events until it drains", queue_size=self._queue_size)
            self._overflowing = True
            self._drop([item])
            return
        self._overflowing = False
        self._queue.append(item)
        self._notify()

    def _notify(self) -> None:
        if self._loop is None or self._wake is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            self._wake.set()
        else:
            self._loop.call_soon_threadsafe(self._wake.set)

    def _drop(self, items: List[Dict[str, Any]]) -> None:
        for item in items:
            if item.get("kind") == "resync":
                self._lost.update(item.get("session_ids") or ())
                continue
            self.dropped += 1
            session_id = item.get("session_id")
            if isinstance(session_id, str) and session_id:
                self._lost.add(session_id)

    # ---- sending -----------------------------------------------------

    async def _run(self) -> None:
        attempt = 0
        while True:
            if self._closing and not self._queue and not self._llm_waits.pending():
                return
            try:
                connection = await self._connect()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - the backend may be restarting
                if self._closing:
                    self._drop(list(self._queue))
                    self._queue.clear()
                    return
                if attempt == 0 or attempt % 10 == 9:
                    logger.warning("Chat relay cannot reach the backend", error=str(error), attempt=attempt + 1)
                await asyncio.sleep(self._retry_delays[min(attempt, len(self._retry_delays) - 1)])
                attempt += 1
                continue
            if attempt:
                logger.info("Chat relay reached the backend", attempts=attempt + 1)
            attempt = 0
            self._llm_waits.reconnect()
            reader = asyncio.create_task(self._drain(connection))
            try:
                await self._pump(connection, reader)
                return
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - reconnect and carry on
                logger.warning("Chat relay connection lost", error=str(error))
            finally:
                reader.cancel()
                with suppress(BaseException):
                    await reader
                with suppress(Exception):
                    await connection.close()

    async def _pump(self, connection: RelayConnection, reader: asyncio.Task) -> None:
        assert self._wake is not None
        while True:
            diagnostic_batch = self._llm_waits.batch()
            if diagnostic_batch:
                await connection.send_str(_dumps({"type": WIRE_TYPE,
                    "items": [{"kind": "llm_wait", "data": item} for _, item in diagnostic_batch]}))
                # Updates made while send_str yielded remain pending. A failed
                # send never consumes diagnostic state; reconnect replays it.
                self._llm_waits.sent(diagnostic_batch)
            if not self._queue and not self._lost:
                if self._llm_waits.pending():
                    continue
                if self._closing:
                    return
                self._wake.clear()
                if self._queue or self._closing or self._llm_waits.pending():
                    continue
                waiter = asyncio.ensure_future(self._wake.wait())
                done, _ = await asyncio.wait({waiter, reader}, return_when=asyncio.FIRST_COMPLETED)
                if reader in done:
                    waiter.cancel()
                    raise ConnectionError("the backend closed the relay connection")
                continue
            batch = [self._queue.popleft() for _ in range(min(self._batch_size, len(self._queue)))]
            resynced = sorted(self._lost)
            if resynced:
                batch.append({"kind": "resync", "session_ids": resynced})
                self._lost.difference_update(resynced)
            try:
                await connection.send_str(_dumps({"type": WIRE_TYPE, "items": batch}))
            except Exception:
                self._drop(batch)
                raise

    @staticmethod
    async def _drain(connection: RelayConnection) -> None:
        """Read what the backend sends back (its answers, pings) so the
        connection stays healthy; returns when it closes."""
        async for _message in connection:
            pass

    async def close(self, timeout: float = 3.0) -> None:
        """Send what is queued (for at most ``timeout`` seconds), then stop."""
        self._closing = True
        self._notify()
        task, self._task = self._task, None
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001 - stopping regardless
            task.cancel()
            with suppress(BaseException):
                await task
        if self._queue:
            self._drop(list(self._queue))
            self._queue.clear()


# ---- the standalone worker's relay ----------------------------------------


class _AiohttpConnection:
    def __init__(self, session: Any, websocket: Any) -> None:
        self._session = session
        self._websocket = websocket

    async def send_str(self, data: str) -> None:
        await self._websocket.send_str(data)

    def __aiter__(self) -> AsyncIterator[Any]:
        return self._websocket.__aiter__()

    async def close(self) -> None:
        try:
            await self._websocket.close()
        finally:
            await self._session.close()


def backend_connector(url: Optional[str] = None) -> Connect:
    """Opens the worker's own ``/ws/internal`` connection, with the worker
    token (``services/authz``)."""

    async def connect() -> RelayConnection:
        import aiohttp

        from core.config import Settings
        from services.authz import internal_socket_headers
        from services.temporal.ws_client import internal_ws_url

        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, connect=10))
        try:
            websocket = await session.ws_connect(
                url or internal_ws_url(),
                headers=internal_socket_headers(Settings().secret_key),
                heartbeat=20,
            )
        except BaseException:
            await session.close()
            raise
        return _AiohttpConnection(session, websocket)

    return connect


_active: Optional[ChatRelay] = None


def active_relay() -> Optional[ChatRelay]:
    """The relay this process sends its chat events through, or None when
    they go straight into this process's hub."""
    return _active


def start_relay(connect: Optional[Connect] = None) -> ChatRelay:
    """Send this process's chat events to the backend from now on. For a
    standalone worker; call from inside its event loop."""
    global _active
    if _active is not None:
        return _active
    relay = ChatRelay(
        connect or backend_connector(),
        queue_size=hub_setting("relay_queue_size"),
        batch_size=hub_setting("relay_batch_size"),
    )
    relay.start()
    _active = relay
    logger.info("Chat events go to the backend through the relay")
    return relay


async def stop_relay() -> None:
    """Send what is queued, then publish locally again."""
    global _active
    relay, _active = _active, None
    if relay is not None:
        await relay.close()
        if relay.dropped:
            logger.warning("Chat relay dropped events", dropped=relay.dropped)


# ---- the backend's side -----------------------------------------------------


def _from_worker(websocket: Any) -> bool:
    scope = getattr(websocket, "scope", None)
    return isinstance(scope, dict) and scope.get("path") == "/ws/internal"


def _identifier(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= _MAX_ID


def _run_event(item: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    workflow_id = item.get("workflow_id")
    fields = item.get("fields")
    event_key = item.get("event_key")
    if not (_identifier(item.get("run_id")) and _identifier(item.get("session_id"))):
        return None
    if item.get("suffix") not in RUN_SUFFIXES:
        return None
    if workflow_id is not None and not _identifier(workflow_id):
        return None
    if fields is not None and not isinstance(fields, dict):
        return None
    if event_key is not None and not isinstance(event_key, str):
        return None
    return {
        "run_id": item["run_id"],
        "session_id": item["session_id"],
        "workflow_id": workflow_id,
        "suffix": item["suffix"],
        "fields": fields or {},
        "event_key": event_key,
    }


def _broadcast(item: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    wire_key = item.get("type")
    data = item.get("data")
    expected = RELAYED_BROADCASTS.get(wire_key) if isinstance(wire_key, str) else None
    if expected is None or not isinstance(data, dict):
        return None
    event_type = data.get("type")
    if not isinstance(event_type, str) or not event_type.startswith(expected):
        return None
    return {"type": wire_key, "data": data}


@ws_handler()
async def handle_chat_run_publish(data: Dict[str, Any], websocket: Any) -> Dict[str, Any]:
    """Publish a standalone worker's chat events here. ``/ws/internal`` only."""
    if not _from_worker(websocket):
        logger.warning("chat_run_publish refused on a client socket")
        return {"success": False, "error": "access_denied"}
    items = data.get("items")
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        return {"success": False, "error": "invalid_items"}

    from services.chat.hub import get_chat_hub
    from services.status_broadcaster import get_status_broadcaster

    hub = get_chat_hub()
    published = refused = 0
    for item in items:
        kind = item.get("kind") if isinstance(item, dict) else None
        try:
            if kind == "run_event" and (event := _run_event(item)) is not None:
                hub.publish(**event)
            elif kind == "broadcast" and (message := _broadcast(item)) is not None:
                await get_status_broadcaster().broadcast(message)
            elif kind == "llm_wait":
                from services.llm_wait import validate_wait
                delta = validate_wait(item.get("data"))
                if delta is None:
                    refused += 1
                    continue
                await get_status_broadcaster().publish_llm_wait(delta, from_relay=True)
            elif kind == "resync" and isinstance(item.get("session_ids"), list):
                for session_id in item["session_ids"]:
                    if _identifier(session_id):
                        hub.resync(session_id)
            else:
                refused += 1
                continue
        except Exception:  # noqa: BLE001 - one bad item never loses the rest
            logger.warning("A relayed chat event could not be published", kind=kind, exc_info=True)
            refused += 1
            continue
        published += 1
    if refused:
        logger.warning("chat_run_publish refused items", refused=refused, published=published)
    return {"success": True, "published": published, "refused": refused}


WS_HANDLERS = {WIRE_TYPE: handle_chat_run_publish}

__all__ = [
    "MAX_ITEMS",
    "RELAYED_BROADCASTS",
    "WIRE_TYPE",
    "WS_HANDLERS",
    "ChatRelay",
    "active_relay",
    "backend_connector",
    "handle_chat_run_publish",
    "start_relay",
    "stop_relay",
]
