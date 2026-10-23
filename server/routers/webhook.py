"""Dynamic webhook endpoint router for incoming HTTP requests.

Two dispatch paths:

1. **Plugin-owned WebhookSource** (Wave 12 framework). Plugins register
   a :class:`services.events.WebhookSource` for their path. Its
   ``handle()`` verifies the signature, shapes a :class:`WorkflowEvent`
   and wakes a waiting canvas Run; the plugin's override emits the event
   to deployed triggers.
2. **Generic webhook.** Paths no plugin claims reach ``webhookTrigger``
   nodes through ``nodes/trigger/webhook_trigger/_routes.deliver_webhook``,
   which answers 404 for a path no running workflow declares, 405 for a
   method none of its triggers accepts and 401 when the header check
   fails.

Both read the body through ``read_body_capped``, so a body larger than
``WEBHOOK_MAX_BODY_BYTES`` is refused with 413 before it reaches a
workflow.
"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from typing import Dict
import asyncio
import logging

from services.events import WEBHOOK_SOURCES
from services.events.webhook import read_body_capped

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook", tags=["webhook"])

# What the generic route answers when ``deliver_webhook`` refuses a request.
_REFUSALS = {
    404: "No running workflow has a webhook at this path.",
    405: "No webhook at this path accepts this method.",
    401: "Missing or wrong authentication header.",
}

# Pending responses: path -> asyncio.Future (for responseNode mode)
_pending_responses: Dict[str, asyncio.Future] = {}


def resolve_webhook_response(node_id: str, response_data: dict):
    """Resolve a pending webhook response.

    Called by webhookResponse node execution to send response back to caller.
    Uses path from response_data to find the pending Future.
    """
    # Find pending response by path (stored when we started waiting)
    for path, future in list(_pending_responses.items()):
        if not future.done():
            future.set_result(response_data)
            logger.info(f"[Webhook] Response resolved for path: {path}")
            return

    logger.warning(f"[Webhook] No pending response found for node: {node_id}")


@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def handle_webhook(path: str, request: Request):
    """Handle incoming webhook requests.

    Path-handler dispatch (Wave 12) runs first; legacy generic
    dispatch is the fallback for paths nobody has claimed.
    """
    source = WEBHOOK_SOURCES.get(path)
    if source is not None:
        try:
            if request.method == "GET":
                # Subscription handshakes (Meta's hub.challenge echo, and
                # equivalents) need a provider-specific body that is not
                # this router's fixed JSON. A source that declines returns
                # None and falls through to the normal handle() path, so
                # sources without an override are unaffected.
                handshake = await source.handle_get(request)
                if handshake is not None:
                    logger.info("[Webhook] GET %s -> %s handshake", path, type(source).__name__)
                    return handshake
            await source.handle(request)
        except HTTPException:
            raise
        except Exception as e:
            logger.exception("[Webhook] %s handler crashed: %s", path, e)
            raise HTTPException(status_code=500, detail=str(e))
        logger.info("[Webhook] %s %s -> %s", request.method, path, type(source).__name__)
        return JSONResponse({"status": "received", "path": path}, status_code=200)

    body = await read_body_capped(request)
    json_body = None
    content_type = request.headers.get("content-type", "")

    if "application/json" in content_type and body:
        try:
            json_body = await request.json()
        except Exception:
            pass

    from core.container import container
    from nodes.trigger.webhook_trigger._routes import deliver_webhook

    status = await deliver_webhook(
        container.database(),
        method=request.method,
        path=path,
        headers=dict(request.headers),
        query=dict(request.query_params),
        body=body.decode("utf-8") if body else "",
        json_body=json_body,
    )
    logger.info(f"[Webhook] {request.method} /webhook/{path} -> {status}")
    if status != 200:
        return JSONResponse({"status": "refused", "path": path, "error": _REFUSALS[status]}, status_code=status)

    return JSONResponse(
        content={"status": "received", "path": path, "message": "Webhook received and dispatched to workflow"}, status_code=200
    )


@router.get("/")
async def list_info():
    """Get webhook endpoint info."""
    return {
        "endpoint": "/webhook/{path}",
        "description": "Send HTTP requests to trigger webhookTrigger nodes",
        "usage": "Deploy a workflow with webhookTrigger node, then send requests to /webhook/{path}",
        "example": "POST /webhook/my-webhook with JSON body",
    }
