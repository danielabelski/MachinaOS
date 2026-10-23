"""Attempt-local diagnostics; never selects transport, retries or streaming."""
from __future__ import annotations

import asyncio
import hashlib
import math
import time
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from functools import wraps

from core.logging import get_logger

logger = get_logger(__name__)
_current = ContextVar("llm_attempt_observer", default=None)
_STAGES = {"preparation", "authentication", "hydration", "routing", "client_acquisition", "client_lock", "client_construction",
           "request_encoding", "sdk_request", "normalization", "delivery", "client_release", "persistence", "retry_wait"}
_META = {"provider_request_id", "transport", "route_source", "cache_hit", "finish_reason", "input_tokens", "output_tokens"}


def warning_seconds():
    from core.env_defaults import env_value
    value = float(env_value("LLM_SLOW_WARNING_SECONDS"))
    if not math.isfinite(value) or value <= 0:
        raise ValueError("LLM_SLOW_WARNING_SECONDS must be positive and finite")
    return value


def observe_stage(stage: str, **metadata):
    observer = _current.get()
    if observer is not None:
        try:
            observer.stage(stage, **metadata)
        except Exception:
            pass


def observe_sdk_request(provider):
    """Read the existing SDK transport without constructing or selecting one."""
    if _current.get() is None:
        return
    transport = "unknown"
    try:
        import httpx
        sdk = getattr(provider, "_client", None)
        if isinstance(getattr(sdk, "_client", None), httpx.AsyncClient):
            transport = "httpx"
    except Exception:
        pass
    observe_stage("sdk_request", transport=transport)


def observe_response(response):
    if _current.get() is None:
        return
    try:
        observe_stage("normalization", provider_request_id=getattr(response, "_request_id", None))
    except Exception:
        observe_stage("normalization")


def observing():
    return _current.get() is not None


class LLMAttemptObserver:
    def __init__(self, payload, *, threshold=None, publish=None, clock=time.monotonic, sleep=asyncio.sleep):
        self.clock = clock
        self.sleep = sleep
        self.started = self.changed = clock()
        self.threshold = threshold if threshold is not None else payload.get("llm_warning_seconds", warning_seconds())
        self.name = "preparation"
        self.durations = {}
        self.metadata = {}
        self.warned = self.closed = False
        self.seq = 0
        self.pending = set()
        self.timer = None
        self.publish = publish or self._publish
        attribution = payload.get("attribution") or {}
        runtime = payload.get("runtime_scope") or {}
        try:
            from temporalio import activity
            info = activity.info()
            execution = str(attribution.get("execution_id") or attribution.get("agent_execution_id") or info.workflow_id)
            attempt, activity_id = info.attempt, info.activity_id
        except RuntimeError:
            execution = str(attribution.get("execution_id") or attribution.get("agent_execution_id") or payload.get("execution_id") or uuid.uuid4().hex)
            attempt, activity_id = 1, "native-llm-step"
        scope = "workspace" if (runtime.get("native_workspace_version") == 1 or runtime.get("invocation_id")
                                or not runtime.get("generation")) else "generation"
        root = str(attribution.get("root_execution_id") or payload.get("root_execution_id") or execution)
        self.identity = {"workflow_id": str(payload.get("workflow_id") or ""), "node_id": str(payload.get("node_id") or ""),
            "agent_node_id": str(payload.get("node_id") or ""), "agent_label": str(payload.get("agent_label") or "Agent")[:256],
            "runtime_behavior_version": 1, "execution_id": execution, "root_execution_id": root,
            "request_id": uuid.uuid4().hex, "scope": scope, "activity_id": activity_id, "attempt": attempt,
            **{key: runtime[key] for key in ("generation", "workspace_epoch", "invocation_id", "thread_id", "turn_id") if runtime.get(key) is not None}}
        self.identity["execution_key"] = runtime.get("execution_key") or hashlib.sha256(
            f"{scope}:{root}:{execution}:{self.identity['node_id']}".encode()).hexdigest()[:32]
        self.labels = {key: str(payload[key])[:128] for key in ("provider", "model") if payload.get(key)}

    async def _publish(self, delta):
        from services.status_broadcaster import get_status_broadcaster
        await get_status_broadcaster().publish_llm_wait(delta)

    def stage(self, name, **metadata):
        if self.closed:
            return
        now = self.clock()
        if name in _STAGES and name != self.name:
            self.durations[self.name] = self.durations.get(self.name, 0) + (now - self.changed) * 1000
            self.name, self.changed = name, now
            if self.warned:
                task = asyncio.create_task(self._emit("waiting"))
                self.pending.add(task)
                task.add_done_callback(self.pending.discard)
        for key, value in metadata.items():
            if key in _META and isinstance(value, (str, int, float, bool)):
                self.metadata[key] = "".join(c for c in value[:160] if c.isprintable()) if isinstance(value, str) else value

    async def _emit(self, state):
        if state == "waiting" and self.closed:
            return
        self.seq += 1
        delta = {**self.identity, "seq": self.seq, "state": state, "stage": self.name,
                 "elapsed_ms": max(0, int((self.clock() - self.started) * 1000)), **self.labels}
        try:
            await asyncio.wait_for(self.publish(delta), timeout=0.25)
        except Exception:
            try:
                logger.debug("llm_wait_publish_failed")
            except Exception:
                pass

    async def _watch(self):
        while (remaining := self.threshold - (self.clock() - self.started)) > 0:
            await self.sleep(remaining)
        if self.closed:
            return
        self.warned = True
        try:
            logger.info("llm_attempt_slow", **self.identity, **self.labels, stage=self.name,
                        elapsed_ms=int((self.clock() - self.started) * 1000))
        except Exception:
            pass
        await self._emit("waiting")

    async def __aenter__(self):
        self.token = _current.set(self)
        self.timer = asyncio.create_task(self._watch())
        return self

    async def __aexit__(self, exc_type, exc, tb):
        now = self.clock()
        self.durations[self.name] = self.durations.get(self.name, 0) + (now - self.changed) * 1000
        self.closed = True
        self.timer.cancel()
        await asyncio.gather(self.timer, *self.pending, return_exceptions=True)
        if self.warned:
            await self._emit("cleared")
        try:
            logger.info("llm_attempt_complete", **self.identity, **self.labels, **self.metadata,
                outcome="cancelled" if exc_type is asyncio.CancelledError else "error" if exc_type else "success",
                duration_ms=int((now - self.started) * 1000), stages_ms={key: round(value, 2) for key, value in self.durations.items()})
        except Exception:
            pass  # Diagnostics must never turn a billed success into a retry.
        finally:
            _current.reset(self.token)


@asynccontextmanager
async def observed_attempt(payload):
    if payload.get("runtime_behavior_version") != 1:
        yield None
        return
    async with LLMAttemptObserver(payload) as observer:
        yield observer


def observed_native_step(func):
    @wraps(func)
    async def call(*args, **kwargs):
        payload = kwargs.pop("observer_payload", None)
        if payload and _current.get() is None:
            async with observed_attempt(payload):
                return await func(*args, **kwargs)
        return await func(*args, **kwargs)
    return call
