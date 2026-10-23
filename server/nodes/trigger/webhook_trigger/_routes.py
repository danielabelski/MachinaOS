"""Deliver a request on ``/webhook/{path}`` to the deployed webhookTrigger
nodes that declare its path.

The route table is the graph snapshot of every running or paused generation
(``list_active_workflow_controls``). A path no generation declares is 404, a
method none of its triggers accepts is 405, and a request that fails the
header check of every trigger that has one is 401. Each workflow with an
accepting trigger gets one event scoped to it, so a webhook never fires
another workflow's trigger, and its body is never broadcast (RFC-0004 C3).
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Any, List, Mapping

from . import WebhookTriggerParams
from ._events import emit_webhook_received

#: Request headers never passed on to a workflow, besides each trigger's own
#: authentication header.
_PRIVATE_HEADERS = frozenset({"authorization", "cookie", "proxy-authorization"})


@dataclass(frozen=True)
class _Route:
    workflow_id: str
    node_id: str
    params: WebhookTriggerParams


async def _routes(database: Any, path: str) -> List[_Route]:
    routes: List[_Route] = []
    if not path:
        return routes
    for control in await database.list_active_workflow_controls():
        snapshot = control.graph_snapshot or {}
        parameters = snapshot.get("parameters") or {}
        for node in snapshot.get("nodes") or []:
            if node.get("type") != "webhookTrigger" or (node.get("data") or {}).get("disabled"):
                continue
            params = WebhookTriggerParams.model_validate(parameters.get(node.get("id")) or {})
            if params.path == path:
                routes.append(_Route(workflow_id=control.workflow_id, node_id=node["id"], params=params))
    return routes


async def _authorized(database: Any, route: _Route, headers: Mapping[str, str]) -> bool:
    if route.params.authentication != "header":
        return True
    # The secret stays out of snapshots and Temporal input; the saved
    # parameters hold it. With none configured the trigger refuses.
    saved = await database.get_node_parameters(route.node_id) or {}
    expected = str(saved.get("header_value") or "")
    received = str(headers.get(route.params.header_name.lower(), ""))
    return bool(expected) and hmac.compare_digest(received.encode(), expected.encode())


async def deliver_webhook(
    database: Any,
    *,
    method: str,
    path: str,
    headers: Mapping[str, str],
    query: Mapping[str, str],
    body: str,
    json_body: Any,
) -> int:
    """Deliver the request; returns the HTTP status to answer with.

    ``headers`` must have lowercase names, as Starlette gives them.
    """
    routes = await _routes(database, path)
    if not routes:
        return 404
    routes = [route for route in routes if route.params.method in ("ALL", method.upper())]
    if not routes:
        return 405
    accepted = [route for route in routes if await _authorized(database, route, headers)]
    if not accepted:
        return 401

    hidden = _PRIVATE_HEADERS | {
        route.params.header_name.lower() for route in routes if route.params.authentication == "header"
    }
    webhook_data = {
        "method": method,
        "path": path,
        "headers": {name: value for name, value in headers.items() if name.lower() not in hidden},
        "query": dict(query),
        "body": body,
        "json": json_body,
    }
    for workflow_id in sorted({route.workflow_id for route in accepted}):
        await emit_webhook_received(webhook_data, workflow_id=workflow_id)
    return 200


__all__ = ["deliver_webhook"]
