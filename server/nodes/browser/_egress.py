"""The egress proxy every managed Chrome sends its traffic through.

Chrome is launched with ``--proxy-server=http://127.0.0.1:<this port>`` and
``--proxy-bypass-list=<-loopback>`` (which removes Chrome's built-in
"localhost goes direct" rule), so every request, WebSocket and all, arrives
here. For each one the proxy resolves the hostname itself, drops every
address the network policy refuses, and connects only to an address it has
checked, so a DNS answer cannot switch to a private address between the
check and the connection. A refused request gets a plain ``403`` page that
names the reason.

It runs on its own thread and event loop: page traffic (images, video) is
bulk byte copying that has no business competing with the backend's loop.
The policy comes from a callable, read on each new connection, because the
Browser node holding a profile decides whether its local network is allowed.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import threading
from typing import Callable, Optional, Tuple
from urllib.parse import urlsplit

from core.logging import get_logger

from services.netpolicy import NetPolicy, address_block_reason, host_block_reason

logger = get_logger(__name__)

_HEADER_LIMIT = 64 * 1024
_CONNECT_TIMEOUT = 15.0
_RELAY_CHUNK = 64 * 1024

PolicyProvider = Callable[[], NetPolicy]


class _Blocked(Exception):
    pass


async def _read_head(reader: asyncio.StreamReader) -> bytes:
    try:
        return await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=30)
    except asyncio.LimitOverrunError:
        raise _Blocked("request headers too large") from None


async def _relay(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await src.read(_RELAY_CHUNK)
            if not chunk:
                break
            dst.write(chunk)
            await dst.drain()
    except (ConnectionError, asyncio.IncompleteReadError, OSError):
        pass
    finally:
        try:
            if dst.can_write_eof():
                dst.write_eof()
        except (OSError, RuntimeError):
            pass


def _split_host_port(authority: str, default_port: int) -> Tuple[str, int]:
    parts = urlsplit(f"//{authority}")
    host = parts.hostname or ""
    try:
        port = parts.port or default_port
    except ValueError:
        raise _Blocked("invalid port") from None
    return host, port


class EgressProxy:
    """One filtering HTTP/CONNECT proxy on ``127.0.0.1:<ephemeral>``."""

    def __init__(self, policy: PolicyProvider, *, name: str = "browser-egress", runtime_version: int = 0) -> None:
        if runtime_version not in (0, 1):
            raise ValueError("Unsupported browser proxy resource version")
        self.runtime_version = runtime_version
        self._policy = policy
        self._name = name
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._server: Optional[asyncio.base_events.Server] = None
        self.port: Optional[int] = None
        self._tasks: set[asyncio.Task] = set()
        self._writers: set[asyncio.StreamWriter] = set()
        self._lifecycle_lock = threading.RLock()
        self._closing = False
        self._drain_timeout = 5.0
        self._halfclose_timeout = 10.0
        self._close_timeout = 1.0

    # -- lifecycle (called from the main loop) ------------------------------

    def start(self) -> int:
        if self.runtime_version == 1:
            return self._start_v1()
        if self.port is not None:
            return self.port
        ready = threading.Event()
        error: list[BaseException] = []

        def _run() -> None:
            loop = asyncio.new_event_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)
            try:
                self._server = loop.run_until_complete(asyncio.start_server(self._handle, host="127.0.0.1", port=0, limit=_HEADER_LIMIT))
                self.port = self._server.sockets[0].getsockname()[1]
            except BaseException as exc:  # noqa: BLE001 - surfaced to start()
                error.append(exc)
                ready.set()
                return
            ready.set()
            try:
                loop.run_forever()
            finally:
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.close()

        self._thread = threading.Thread(target=_run, name=self._name, daemon=True)
        self._thread.start()
        ready.wait(timeout=10)
        if error:
            raise error[0]
        if self.port is None:
            raise RuntimeError("the browser egress proxy did not start")
        logger.debug("[browser] egress proxy listening on 127.0.0.1:%s", self.port)
        return self.port

    def stop(self) -> None:
        if self.runtime_version == 1:
            return self._stop_v1()
        loop, server = self._loop, self._server
        if loop is None:
            return

        async def _shutdown() -> None:
            if server is not None:
                server.close()
                await server.wait_closed()
            pending = [task for task in asyncio.all_tasks(loop) if task is not asyncio.current_task()]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            loop.call_soon(loop.stop)

        try:
            future = asyncio.run_coroutine_threadsafe(_shutdown(), loop)
            future.result(timeout=5)
        except (RuntimeError, TimeoutError):
            pass
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._loop = self._server = self._thread = None
        self.port = None

    def _start_v1(self) -> int:
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                if self._closing or self.port is None:
                    raise RuntimeError("Browser proxy cleanup has not completed")
                return self.port
            self._closing = False
            ready = threading.Event()
            errors = []

            def run():
                loop = asyncio.new_event_loop()
                self._loop = loop
                asyncio.set_event_loop(loop)
                try:
                    self._server = loop.run_until_complete(asyncio.start_server(
                        self._handle, host="127.0.0.1", port=0, limit=_HEADER_LIMIT))
                    self.port = self._server.sockets[0].getsockname()[1]
                    ready.set()
                    loop.run_forever()
                except BaseException as exc:
                    errors.append(exc)
                    ready.set()
                finally:
                    loop.run_until_complete(loop.shutdown_asyncgens())
                    loop.close()

            self._thread = threading.Thread(target=run, name=self._name, daemon=True)
            self._thread.start()
            if not ready.wait(timeout=10):
                self._closing = True
                raise RuntimeError("Browser proxy startup did not complete")
            if errors:
                raise errors[0]
            return self.port

    async def _close_writer(self, writer):
        try:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), timeout=self._close_timeout)
        except (OSError, RuntimeError, TimeoutError):
            transport = getattr(writer, "transport", None)
            if transport is not None:
                transport.abort()
        finally:
            self._writers.discard(writer)

    def _stop_v1(self) -> bool:
        with self._lifecycle_lock:
            thread, loop = self._thread, self._loop
            if thread is None:
                return True
            if not thread.is_alive():
                self._loop = self._server = self._thread = None
                self.port = None
                return True
            self._closing = True

            async def shutdown():
                if self._server is not None:
                    self._server.close()
                tasks = [t for t in self._tasks if t is not asyncio.current_task()]
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*(self._close_writer(w) for w in tuple(self._writers)), return_exceptions=True)
                if tasks:
                    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=3)
                if self._server is not None:
                    await asyncio.wait_for(self._server.wait_closed(), timeout=1)
                loop.call_soon(loop.stop)

            try:
                if loop is None or loop.is_closed():
                    return False
                future = asyncio.run_coroutine_threadsafe(shutdown(), loop)
                future.result(timeout=5)
                thread.join(timeout=2)
            except Exception:
                # Keep every handle: a caller may retry cleanup but must not
                # replace this still-live proxy with a second resource.
                logger.warning("[browser] proxy cleanup is still pending")
                return False
            if thread.is_alive():
                return False
            self._loop = self._server = self._thread = None
            self.port = None
            self._tasks.clear()
            self._writers.clear()
            return True

    async def _relay_v1(self, src, dst):
        try:
            while True:
                data = await src.read(_RELAY_CHUNK)
                if not data:
                    if dst.can_write_eof():
                        dst.write_eof()
                        await asyncio.wait_for(dst.drain(), timeout=self._drain_timeout)
                    return "eof"
                dst.write(data)
                await asyncio.wait_for(dst.drain(), timeout=self._drain_timeout)
        except (ConnectionError, OSError, TimeoutError, RuntimeError):
            return "error"

    async def _relay_pair(self, client_reader, client_writer, upstream_reader, upstream_writer):
        tasks = {asyncio.create_task(self._relay_v1(client_reader, upstream_writer)),
                 asyncio.create_task(self._relay_v1(upstream_reader, client_writer))}
        self._tasks.update(tasks)
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if all(task.result() == "eof" for task in done) and pending:
                # TCP half-close is valid: let the other direction finish its
                # response, but never retain an idle tunnel indefinitely.
                await asyncio.wait_for(asyncio.gather(*pending), timeout=self._halfclose_timeout)
        except TimeoutError:
            pass
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._tasks.difference_update(tasks)

    # -- connection handling (proxy thread) ---------------------------------

    async def _resolve_allowed(self, host: str, port: int, policy: NetPolicy) -> list[str]:
        reason = host_block_reason(host, policy)
        if reason:
            raise _Blocked(reason)
        try:
            literal = ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            literal = None
        if literal is not None:
            candidates = [literal]
        else:
            loop = asyncio.get_running_loop()
            try:
                infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            except socket.gaierror:
                raise _Blocked(f"{host} could not be resolved") from None
            candidates = list(dict.fromkeys(ipaddress.ip_address(info[4][0].split("%", 1)[0]) for info in infos))
        last_reason = "no address"
        allowed = []
        for address in candidates:
            reason = address_block_reason(address, port, policy)
            if reason is None:
                allowed.append(str(address))
            else:
                last_reason = reason
        if allowed:
            return allowed
        raise _Blocked(last_reason)

    async def _open(self, host: str, port: int, policy: NetPolicy) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        addresses = await self._resolve_allowed(host, port, policy)
        try:
            async with asyncio.timeout(_CONNECT_TIMEOUT):
                last_error = OSError("no reachable address")
                for address in addresses:
                    try:
                        # localhost may resolve to IPv6 first while the app
                        # listens only on IPv4. Try only addresses already
                        # checked above; never resolve the hostname again.
                        opened = await asyncio.open_connection(address, port)
                        if self.runtime_version == 1:
                            self._writers.add(opened[1])
                        return opened
                    except OSError as exc:
                        last_error = exc
                raise last_error
        except (OSError, asyncio.TimeoutError) as exc:
            raise ConnectionError(f"could not connect to {host}:{port}: {exc}") from exc

    async def _drain(self, writer):
        if self.runtime_version == 1:
            await asyncio.wait_for(writer.drain(), timeout=self._drain_timeout)
        else:
            await writer.drain()

    async def _refuse(self, writer: asyncio.StreamWriter, status: str, reason: str) -> None:
        body = f"Blocked by OpenCompany: {reason}\n".encode("utf-8", errors="replace")
        head = (
            f"HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
        ).encode("ascii")
        try:
            writer.write(head + body)
            await self._drain(writer)
        except (ConnectionError, OSError):
            pass

    async def _handle(self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
        current = asyncio.current_task()
        if self.runtime_version == 1:
            self._tasks.add(current)
            self._writers.add(client_writer)
        upstream_writer: Optional[asyncio.StreamWriter] = None
        try:
            head = await _read_head(client_reader)
            request_line, _, rest = head.partition(b"\r\n")
            parts = request_line.decode("latin-1").split(" ")
            if len(parts) != 3:
                await self._refuse(client_writer, "400 Bad Request", "malformed request")
                return
            method, target, version = parts
            policy = self._policy()

            if method.upper() == "CONNECT":
                host, port = _split_host_port(target, 443)
                upstream_reader, upstream_writer = await self._open(host, port, policy)
                client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await self._drain(client_writer)
            else:
                url = urlsplit(target)
                if url.scheme.lower() != "http" or not url.hostname:
                    await self._refuse(client_writer, "400 Bad Request", "only absolute http:// requests are proxied")
                    return
                host, port = _split_host_port(url.netloc, 80)
                upstream_reader, upstream_writer = await self._open(host, port, policy)
                path = url.path or "/"
                if url.query:
                    path += "?" + url.query
                headers = []
                upgrade = False
                for line in rest.split(b"\r\n"):
                    if not line:
                        continue
                    name = line.split(b":", 1)[0].strip().lower()
                    if name in (b"proxy-connection", b"proxy-authorization"):
                        continue
                    if name == b"upgrade":
                        upgrade = True
                    if name in (b"connection", b"keep-alive"):
                        continue
                    headers.append(line)
                # One request per upstream connection: a kept-alive client
                # connection could otherwise send the next request, for any
                # host, down this already-checked socket.
                headers.append(b"Connection: " + (b"Upgrade" if upgrade else b"close"))
                upstream_writer.write(f"{method} {path} {version}\r\n".encode("latin-1") + b"\r\n".join(headers) + b"\r\n\r\n")
                await self._drain(upstream_writer)

            if self.runtime_version == 1:
                await self._relay_pair(client_reader, client_writer, upstream_reader, upstream_writer)
            else:
                await asyncio.gather(_relay(client_reader, upstream_writer), _relay(upstream_reader, client_writer))
        except _Blocked as exc:
            await self._refuse(client_writer, "403 Forbidden", str(exc))
        except ConnectionError as exc:
            await self._refuse(client_writer, "502 Bad Gateway", str(exc))
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, OSError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one bad request must not stop the proxy
            logger.debug("[browser] egress proxy request failed", exc_info=True)
        finally:
            if self.runtime_version == 1:
                await asyncio.gather(*(self._close_writer(w) for w in (upstream_writer, client_writer) if w is not None), return_exceptions=True)
                self._tasks.discard(current)
            for writer in (() if self.runtime_version == 1 else (upstream_writer, client_writer)):
                if writer is not None:
                    try:
                        writer.close()
                    except (OSError, RuntimeError):
                        pass


__all__ = ["EgressProxy", "PolicyProvider"]
