"""Dodo Payments webhook source: the HTTP receiver behind
``POST /webhook/dodo-payments``.

Dodo signs every delivery per Standard Webhooks
(https://docs.dodopayments.com/developer-resources/webhooks): the
``webhook-id``, ``webhook-timestamp`` and ``webhook-signature`` headers, an
HMAC-SHA256 under the endpoint's ``whsec_`` secret saved in Credentials ->
Dodo Payments. ``StandardWebhooksVerifier`` checks it, refusing a timestamp
outside the spec's five-minute window, and the base ``WebhookSource`` fails
closed: 503 without a secret, 400 on a bad signature. The source then shapes
the event into the trigger's output and hands it to both the canvas-Run
waiter and deployed listeners.

A module-level singleton (``get_webhook_source``) plugs into the framework
registry from ``__init__.py``, so reloading the package re-registers the
same instance.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import Request

from services.events import StandardWebhooksVerifier, WebhookSource, WorkflowEvent

from ._credentials import WEBHOOK_KEY_FIELD, DodoPaymentsCredential
from ._events import dodo_payments_event_received, emit_dodo_payments_event


def shape_dodo_payments_event(payload: Dict[str, Any], *, event_id: str) -> Dict[str, Any]:
    """Flatten one Dodo delivery (``{business_id, type, timestamp, data}``)
    into the trigger's output.

    Shaped here rather than in the node's ``shape_output``: a deployed trigger
    hands downstream nodes ``event.data`` verbatim, so shaping on the node
    would make Run and deploy disagree about the fields.
    """
    data = payload.get("data")
    if not isinstance(data, dict):
        data = {}
    return {
        "event_id": event_id,
        "event_type": payload.get("type") or "unknown",
        "business_id": payload.get("business_id"),
        "timestamp": payload.get("timestamp"),
        "payload_type": data.get("payload_type"),
        "data": data,
    }


class DodoPaymentsWebhookSource(WebhookSource):
    """HTTP receiver for Dodo Payments webhook deliveries."""

    type = "dodo_payments.webhook"
    path = "dodo-payments"
    verifier = StandardWebhooksVerifier
    secret_field = WEBHOOK_KEY_FIELD
    credential = DodoPaymentsCredential

    async def shape(self, request: Request, body: bytes, payload: dict) -> WorkflowEvent:
        # The verifier has already required the header.
        return dodo_payments_event_received(
            shape_dodo_payments_event(payload, event_id=request.headers.get("webhook-id") or "")
        )

    async def handle(self, request: Request) -> WorkflowEvent:
        # The base class verifies the signature, shapes the event and wakes a
        # waiting canvas Run (and the Temporal-off deploy path). Deployed
        # triggers on Temporal are reached only through emit.
        event = await super().handle(request)
        await emit_dodo_payments_event(event)
        return event


_webhook: Optional[DodoPaymentsWebhookSource] = None


def get_webhook_source() -> DodoPaymentsWebhookSource:
    global _webhook
    if _webhook is None:
        _webhook = DodoPaymentsWebhookSource()
    return _webhook


__all__ = ["DodoPaymentsWebhookSource", "get_webhook_source", "shape_dodo_payments_event"]
