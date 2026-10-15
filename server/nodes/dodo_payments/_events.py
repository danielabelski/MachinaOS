"""CloudEvents factory and the dispatch wrapper for Dodo Payments webhook
deliveries.

Three things here are load-bearing.

**Every Dodo event travels under one CloudEvents type.** A deployed trigger
listens for exactly one type: ``register_canary_trigger_type`` records one
string per node type, and both the listener's ``EventType`` Search Attribute
and the workflow controller's ``on_event`` match it exactly. The Dodo type
(``payment.succeeded``, ``subscription.renewed``, ...) rides in ``subject``
and in ``data["event_type"]``, which is where the node's filter reads it.

**``WebhookSource.handle`` does not reach deployed listeners.** It feeds the
in-process event waiter, which serves a canvas Run and the Temporal-off
deploy path. Deployed triggers on Temporal are reached only by
``services.events.dispatch.emit``, hence :func:`emit_dodo_payments_event`.

**Nothing is broadcast to the browser.** ``emit`` would otherwise send the
envelope to every connected socket; a payment event carries a customer's
name and email, and no client consumes it, so only the workflows hear it.
"""

from __future__ import annotations

from typing import Any, Mapping

from services.events.envelope import WorkflowEvent

# The exact string ``register_canary_trigger_type`` records for dodoPaymentsReceive.
DODO_PAYMENTS_EVENT_RECEIVED_TYPE = "com.opencompany.dodo_payments.event.received"


def dodo_payments_event_received(data: Mapping[str, Any]) -> WorkflowEvent:
    """One verified Dodo delivery.

    The id is the delivery's ``webhook-id``, which stays the same when Dodo
    retries a delivery, so the deployed listener drops a retry as a
    duplicate. ``workflow_id`` stays unset: a Dodo event is meant to reach
    every deployment that carries the trigger.
    """
    return WorkflowEvent(
        id=str(data.get("event_id") or ""),
        source=f"dodopayments://{data.get('business_id') or 'default'}",
        type=DODO_PAYMENTS_EVENT_RECEIVED_TYPE,
        subject=str(data.get("event_type") or "") or None,
        data=dict(data),
    )


async def emit_dodo_payments_event(event: WorkflowEvent) -> None:
    from services.events.dispatch import emit

    await emit(event, broadcast=False)


__all__ = [
    "DODO_PAYMENTS_EVENT_RECEIVED_TYPE",
    "dodo_payments_event_received",
    "emit_dodo_payments_event",
]
