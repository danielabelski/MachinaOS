import asyncio
from unittest.mock import AsyncMock

import pytest

from services.llm.observation import LLMAttemptObserver, observed_attempt, observe_stage
from services.llm_wait import LLMWaitRegistry, validate_wait


def delta(**changes):
    return {"workflow_id": "wf", "node_id": "agent", "execution_key": "exec", "request_id": "request",
        "execution_id": "run", "root_execution_id": "root", "runtime_behavior_version": 1,
        "seq": 1, "state": "waiting", "stage": "sdk_request", "elapsed_ms": 5000,
        "scope": "generation", "generation": 2, **changes}


class Clock:
    def __init__(self):
        self.now = 0
        self.waits = []

    def __call__(self):
        return self.now

    async def sleep(self, delay):
        future = asyncio.get_running_loop().create_future()
        self.waits.append((self.now + delay, future))
        await future

    def advance(self, seconds):
        self.now += seconds
        for deadline, future in self.waits:
            if deadline <= self.now and not future.done():
                future.set_result(None)


async def flush():
    for _ in range(8):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_five_second_boundary_and_actual_stage_then_clear():
    clock, events = Clock(), []
    async def publish(value):
        events.append(value)
    observer = LLMAttemptObserver({"workflow_id": "wf", "node_id": "agent", "provider": "gemini"},
        threshold=5, clock=clock, sleep=clock.sleep, publish=publish)
    async with observer:
        observe_stage("authentication")
        await flush()
        clock.advance(4.999)
        await flush()
        assert events == []
        clock.advance(0.001)
        await flush()
        assert events[-1]["stage"] == "authentication"
        assert events[-1]["elapsed_ms"] == 5000
        observe_stage("sdk_request", transport="aiohttp")
        await flush()
        assert events[-1]["stage"] == "sdk_request"
    assert events[-1]["state"] == "cleared"
    assert [item["seq"] for item in events] == sorted({item["seq"] for item in events})
    assert observer.metadata == {"transport": "aiohttp"}


@pytest.mark.asyncio
async def test_legacy_constructs_no_observer(monkeypatch):
    import services.llm.observation as module
    def forbidden(*args, **kwargs):
        raise AssertionError("legacy must not start an observer")
    monkeypatch.setattr(module, "LLMAttemptObserver", forbidden)
    async with observed_attempt({}):
        observe_stage("sdk_request")


@pytest.mark.asyncio
async def test_diagnostics_failure_preserves_billed_result(monkeypatch):
    import services.llm.observation as module
    clock = Clock()
    def broken_log(*args, **kwargs):
        raise RuntimeError("logger down")
    monkeypatch.setattr(module.logger, "info", broken_log)
    monkeypatch.setattr(module.logger, "debug", broken_log)
    observer = LLMAttemptObserver({"node_id": "agent"}, threshold=5, clock=clock, sleep=clock.sleep,
        publish=AsyncMock(side_effect=RuntimeError("socket down")))
    async with observer:
        await flush()
        clock.advance(5)
        await flush()
        result = "already paid"
    assert result == "already paid"


@pytest.mark.asyncio
async def test_native_observer_does_not_change_provider_arguments_or_count():
    from services.agent_runtime import run_native_llm_step
    from services.llm.protocol import LLMResponse, Message
    provider = type("Provider", (), {"chat": AsyncMock(return_value=LLMResponse(content="ok"))})()
    arguments = dict(provider="openai", api_key="test", messages=[Message(role="user", content="hi")],
                     model="model", temperature=0.7, max_tokens=20)
    await run_native_llm_step(provider, **arguments)
    legacy_kwargs = provider.chat.call_args.kwargs
    await run_native_llm_step(provider, **arguments, observer_payload={"runtime_behavior_version": 1,
        "workflow_id": "wf", "node_id": "agent"})
    assert provider.chat.call_count == 2
    assert provider.chat.call_args.kwargs == legacy_kwargs
    assert provider.chat.call_args.kwargs["on_event"] is None


def test_projection_rejects_old_clear_and_late_wait():
    registry = LLMWaitRegistry()
    assert registry.apply(delta(seq=2))
    assert registry.apply(delta(seq=1, state="cleared")) is None
    assert registry.apply(delta(seq=3, state="cleared"))
    assert registry.apply(delta(seq=4)) is None
    assert registry.snapshot()[0]["state"] == "cleared"


def test_concurrent_threads_and_workspace_are_independent():
    registry = LLMWaitRegistry()
    registry.apply(delta())
    registry.apply(delta(execution_key="other", request_id="other", execution_id="other-run", thread_id="customer2"))
    registry.apply(delta(execution_key="direct", request_id="direct", scope="workspace", invocation_id="task", workspace_epoch=1))
    registry.close("wf", node_id="agent", execution_id="other-run")
    active = [item for item in registry.snapshot() if item["state"] == "waiting"]
    assert {item["request_id"] for item in active} == {"request", "direct"}
    registry.close("wf", generation=2, scope="generation")
    assert [item["request_id"] for item in registry.snapshot() if item["state"] == "waiting"] == ["direct"]
    assert registry.apply(delta(request_id="late")) is None
    registry.resume_generation("wf", 2)
    assert registry.apply(delta(request_id="new", seq=1))
    assert registry.apply(delta(seq=100)) is None


def test_workspace_reset_does_not_clear_a_new_epoch():
    registry = LLMWaitRegistry()
    registry.apply(delta(scope="workspace", workspace_epoch=1))
    registry.apply(delta(scope="workspace", workspace_epoch=2, request_id="new", execution_key="new"))
    registry.close("wf", scope="workspace", workspace_epoch=1)
    assert registry.apply(delta(scope="workspace", workspace_epoch=1, request_id="late")) is None
    assert [item["request_id"] for item in registry.snapshot() if item["state"] == "waiting"] == ["new"]


@pytest.mark.parametrize("change", [{"seq": True}, {"elapsed_ms": -1}, {"scope": "unknown"},
    {"stage": "prompt text"}, {"runtime_behavior_version": 0}, {"attempt": 0},
    {"generation": "2"}, {"workspace_epoch": -1}, {"thread_id": ["invalid"]}])
def test_public_projection_validation(change):
    assert validate_wait(delta(**change)) is None


@pytest.mark.asyncio
async def test_cancellation_clears_only_its_attempt_and_preserves_error():
    clock, events = Clock(), []
    async def publish(item):
        events.append(item)
    observer = LLMAttemptObserver({"workflow_id": "wf", "node_id": "agent",
        "execution_id": "direct-task", "runtime_scope": {"invocation_id": "task", "workspace_epoch": 1}},
        threshold=5, clock=clock, sleep=clock.sleep, publish=publish)
    with pytest.raises(asyncio.CancelledError):
        async with observer:
            await flush()
            clock.advance(5)
            await flush()
            raise asyncio.CancelledError()
    assert [item["state"] for item in events] == ["waiting", "cleared"]
    assert events[0]["request_id"] == events[1]["request_id"]
    assert events[0]["scope"] == "workspace"


def test_native_without_deployment_scope_is_workspace_work():
    direct = LLMAttemptObserver({"workflow_id": "wf", "node_id": "agent", "execution_id": "direct"})
    deployed = LLMAttemptObserver({"workflow_id": "wf", "node_id": "agent", "execution_id": "deployed",
        "runtime_scope": {"generation": 3}})
    assert direct.identity["scope"] == "workspace"
    assert deployed.identity["scope"] == "generation"


@pytest.mark.asyncio
async def test_retry_attempts_keep_distinct_request_identity(monkeypatch):
    import temporalio.activity
    from types import SimpleNamespace
    monkeypatch.setattr(temporalio.activity, "info", lambda: SimpleNamespace(
        workflow_id="agent-child", activity_id="llm-step-1", attempt=1))
    one = LLMAttemptObserver({"workflow_id": "wf", "node_id": "agent"}, threshold=5)
    monkeypatch.setattr(temporalio.activity, "info", lambda: SimpleNamespace(
        workflow_id="agent-child", activity_id="llm-step-1", attempt=2))
    two = LLMAttemptObserver({"workflow_id": "wf", "node_id": "agent"}, threshold=5)
    assert one.identity["execution_key"] == two.identity["execution_key"]
    assert one.identity["request_id"] != two.identity["request_id"]
    assert (one.identity["attempt"], two.identity["attempt"]) == (1, 2)


def test_temporal_warning_uses_captured_execution_identity(monkeypatch):
    import temporalio.activity
    from types import SimpleNamespace
    monkeypatch.setattr(temporalio.activity, "info", lambda: SimpleNamespace(
        workflow_id="temporal-child", activity_id="llm-step-1", attempt=1))
    observer = LLMAttemptObserver({"workflow_id": "wf", "node_id": "agent",
        "attribution": {"execution_id": "stable-execution", "root_execution_id": "root"}})
    assert observer.identity["execution_id"] == "stable-execution"


def test_sibling_completion_never_closes_another_wait_with_shared_root():
    registry = LLMWaitRegistry()
    registry.apply(delta(execution_key="sibling-a"))
    registry.apply(delta(execution_key="sibling-b"))
    registry.close("wf", node_id="agent", execution_key="sibling-a")
    assert [item["execution_key"] for item in registry.snapshot() if item["state"] == "waiting"] == ["sibling-b"]
    # A terminal event can precede its first delayed diagnostic delta.
    registry.close("wf", node_id="agent", execution_key="sibling-c")
    assert registry.apply(delta(execution_key="sibling-c")) is None
    assert LLMAttemptObserver({"node_id": "agent", "runtime_scope": {"execution_key": "sibling-a"}}).identity["execution_key"] == "sibling-a"
