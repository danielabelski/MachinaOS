"""Connection failures on httpRequest are user/LLM-correctable, not bugs.

A dead target (dev server not yet started) used to raise raw
``httpx.ConnectError`` and log a full double traceback; the NodeUserError
contract is one WARN line plus a structured envelope the agent can act on.
"""

from __future__ import annotations

import httpx
import pytest

from nodes.utility.http_request import HttpRequestNode, HttpRequestParams
from services.plugin import NodeContext, NodeUserError


async def test_connect_error_surfaces_as_node_user_error(monkeypatch) -> None:
    async def refuse(self, *args, **kwargs):
        raise httpx.ConnectError("All connection attempts failed")

    monkeypatch.setattr(httpx.AsyncClient, "request", refuse)
    ctx = NodeContext(node_id="h1", node_type="httpRequest")
    with pytest.raises(NodeUserError) as excinfo:
        await HttpRequestNode().request(ctx, HttpRequestParams(method="GET", url="http://localhost:5173"))
    assert "localhost:5173" in str(excinfo.value)


async def test_timeout_surfaces_as_node_user_error(monkeypatch) -> None:
    async def stall(self, *args, **kwargs):
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(httpx.AsyncClient, "request", stall)
    ctx = NodeContext(node_id="h1", node_type="httpRequest")
    with pytest.raises(NodeUserError) as excinfo:
        await HttpRequestNode().request(ctx, HttpRequestParams(method="GET", url="http://localhost:5173", timeout=2))
    assert "timed out" in str(excinfo.value)
