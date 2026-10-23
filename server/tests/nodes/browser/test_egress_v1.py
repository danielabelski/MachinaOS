"""Controlled local sockets only; no Chrome or external network."""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nodes.browser._egress import EgressProxy
from services.netpolicy import NetPolicy


async def test_private_proxy_preserves_tcp_half_close_and_drains_every_handle():
    async def respond(reader, writer):
        data = await reader.read()
        writer.write(b"reply:" + data)
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    upstream = await asyncio.start_server(respond, "127.0.0.1", 0)
    upstream_port = upstream.sockets[0].getsockname()[1]
    proxy = EgressProxy(lambda: NetPolicy(), runtime_version=1)
    try:
        port = await asyncio.to_thread(proxy.start)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"CONNECT 127.0.0.1:{upstream_port} HTTP/1.1\r\n\r\n".encode())
        await writer.drain()
        assert b"200" in await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
        writer.write(b"payload")
        await writer.drain()
        writer.write_eof()
        assert await asyncio.wait_for(reader.read(), 2) == b"reply:payload"
        writer.close()
        await writer.wait_closed()
    finally:
        assert await asyncio.to_thread(proxy.stop) is True
        upstream.close()
        await upstream.wait_closed()
    assert not proxy._tasks and not proxy._writers and proxy._thread is None
    assert await asyncio.to_thread(proxy.stop) is True


async def test_backpressured_writer_is_bounded_and_close_precedes_wait_closed():
    proxy = EgressProxy(lambda: NetPolicy(), runtime_version=1)
    proxy._drain_timeout = proxy._close_timeout = 0.01
    reader = asyncio.StreamReader()
    reader.feed_data(b"x")
    events = []
    class Writer:
        transport = SimpleNamespace(abort=lambda: events.append("abort"))
        def write(self, _): pass
        async def drain(self): await asyncio.sleep(100)
        def close(self): events.append("close")
        async def wait_closed(self): await asyncio.sleep(100)
    writer = Writer()
    assert await proxy._relay_v1(reader, writer) == "error"
    await proxy._close_writer(writer)
    assert events == ["close", "abort"]


async def test_stop_drains_an_active_idle_tunnel_before_waiting_for_server_close():
    finished = asyncio.Event()
    async def respond(reader, writer):
        try:
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()
            finished.set()
    upstream = await asyncio.start_server(respond, "127.0.0.1", 0)
    proxy = EgressProxy(lambda: NetPolicy(), runtime_version=1)
    try:
        port = await asyncio.to_thread(proxy.start)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        target = upstream.sockets[0].getsockname()[1]
        writer.write(f"CONNECT 127.0.0.1:{target} HTTP/1.1\r\n\r\n".encode())
        await writer.drain()
        await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
        assert await asyncio.wait_for(asyncio.to_thread(proxy.stop), 4) is True
        assert await asyncio.wait_for(reader.read(), 2) == b""
        writer.close()
        await writer.wait_closed()
        await asyncio.wait_for(finished.wait(), 2)
        assert not proxy._writers and not proxy._tasks
    finally:
        await asyncio.to_thread(proxy.stop)
        upstream.close()
        await upstream.wait_closed()


def test_failed_stop_retains_live_resource_for_retry(monkeypatch):
    proxy = EgressProxy(lambda: NetPolicy(), runtime_version=1)
    thread = SimpleNamespace(is_alive=lambda: True)
    loop = SimpleNamespace(is_closed=lambda: False)
    proxy._thread, proxy._loop, proxy.port = thread, loop, 1234
    def submit(coro, _loop):
        coro.close()
        return SimpleNamespace(result=lambda **_: (_ for _ in ()).throw(TimeoutError()))
    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", submit)
    assert proxy.stop() is False
    assert proxy._thread is thread and proxy._loop is loop and proxy.port == 1234
    with pytest.raises(RuntimeError, match="cleanup"):
        proxy.start()


async def test_private_runtime_teardown_runs_blocking_cleanup_off_loop_and_keeps_failed_proxy():
    from nodes.browser._runtime import BrowserRuntime
    from services.plugin import NodeUserError
    runtime = BrowserRuntime()
    threads = []
    profile = SimpleNamespace(id="private", runtime_version=1)
    proxy = SimpleNamespace(runtime_version=1, stop=lambda: threads.append(threading.get_ident()) or False)
    running = SimpleNamespace(profile=profile, cli=SimpleNamespace(stop_daemon=lambda: threads.append(threading.get_ident())),
        chrome=SimpleNamespace(shutdown=AsyncMock(), stop=AsyncMock(), is_running=lambda: False))
    runtime._profiles[profile.id] = running
    runtime._proxies[profile.id] = proxy
    with pytest.raises(NodeUserError, match="cleanup"):
        await runtime._teardown(running)
    assert runtime._proxies[profile.id] is proxy
    assert threads and all(t != threading.get_ident() for t in threads)
    proxy.stop = lambda: True
    await runtime._teardown(running)
    assert profile.id not in runtime._proxies and profile.id not in runtime._profiles
    assert profile.id not in runtime._cleanup_pending


async def test_private_stop_blocks_reopen_until_events_finish(monkeypatch):
    from nodes.browser._profiles import ProfileStore
    from nodes.browser._runtime import BrowserRuntime
    from services.plugin import NodeUserError
    fleet = BrowserRuntime()
    profile = SimpleNamespace(id="private", owner_id="owner", kind="agent", runtime_version=1)
    monkeypatch.setattr("services.plugin.deps.get_database", lambda: object())
    monkeypatch.setattr(ProfileStore, "get", AsyncMock(return_value=profile))
    new_proxy = SimpleNamespace(runtime_version=1, stop=lambda: pytest.fail("new proxy was stopped"))
    async def emit(*_):
        with pytest.raises(NodeUserError, match="cleanup"):
            await fleet.open(profile)
        # A later, distinct resource must never be removed by an old stop tail.
        fleet._proxies[profile.id] = new_proxy
    controller = SimpleNamespace(release_lease=AsyncMock(), sensitive_login=False,
        tabs={}, active_target_id=None, emit=emit)
    running = SimpleNamespace(profile=profile, controller=controller,
        cli=SimpleNamespace(stop_daemon=lambda: None),
        chrome=SimpleNamespace(shutdown=AsyncMock(), is_running=lambda: False))
    fleet._profiles[profile.id] = running
    fleet._proxies[profile.id] = SimpleNamespace(runtime_version=1, stop=lambda: True)
    await fleet.stop_profile(profile.id)
    assert fleet._proxies[profile.id] is new_proxy
    assert profile.id not in fleet._stopping


async def test_shutdown_cancels_private_start_before_final_resource_cleanup():
    from nodes.browser._runtime import BrowserRuntime
    fleet = BrowserRuntime()
    started = asyncio.Event()
    stopped = []
    async def starting():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            fleet._proxies["private"] = SimpleNamespace(runtime_version=1, stop=lambda: stopped.append(True) or True)
    task = asyncio.create_task(starting())
    fleet._starting["private"] = task
    fleet._resource_versions["private"] = 1
    await started.wait()
    await fleet.shutdown()
    assert task.cancelled() and stopped == [True]
    assert not fleet._proxies


async def test_shutdown_keeps_failed_private_proxy_and_reports_fencing_required():
    from nodes.browser._runtime import BrowserRuntime
    from services.plugin import NodeUserError
    fleet = BrowserRuntime()
    proxy = SimpleNamespace(runtime_version=1, stop=lambda: False)
    fleet._proxies["private"] = proxy
    with pytest.raises(NodeUserError, match="fenced"):
        await fleet.shutdown()
    assert fleet._proxies["private"] is proxy
