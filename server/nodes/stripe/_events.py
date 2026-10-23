"""CloudEvents factory and the dispatch wrapper for Stripe webhook deliveries.

Two things here are load-bearing.

**Every Stripe event travels under one CloudEvents type.** A deployed trigger
listens for exactly one type: ``register_canary_trigger_type`` records one
string per node type, and both the listener's ``EventType`` Search Attribute
and the workflow controller's ``on_event`` match it exactly. Envelopes typed
per Stripe event (``stripe.charge.succeeded`` and so on) could never reach a
deployed ``stripeReceive``. The Stripe type rides in ``subject`` and in
``data["event_type"]``, which is where the node's filter reads it.

**``WebhookSource.handle`` does not reach deployed listeners.** It feeds the
in-process event waiter, which serves a canvas Run and the Temporal-off
deploy path. Deployed triggers on Temporal are reached only by
``services.events.dispatch.emit``, hence :func:`emit_stripe_event`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Optional

from services.events.envelope import WorkflowEvent

# The exact string ``register_canary_trigger_type`` records for stripeReceive.
STRIPE_EVENT_RECEIVED_TYPE = "com.opencompany.stripe.event.received"

# Outer wire key ``emit`` names the event with. Not broadcast: a Stripe
# event carries customer details and no client reads it.
_WIRE_KEY = "stripe_event_received"


def stripe_event_received(
    data: Mapping[str, Any],
    *,
    event_id: str,
    account: Optional[str],
    time: datetime,
) -> WorkflowEvent:
    """One Stripe event forwarded by ``stripe listen``.

    ``event_id`` is Stripe's ``evt_`` id, which stays the same when Stripe
    redelivers, so the deployed listener drops a redelivery as a duplicate.
    ``workflow_id`` stays unset: a Stripe event is meant to reach every
    deployment that carries the trigger.
    """
    return WorkflowEvent(
        id=event_id,
        source=f"stripe://{account or 'default'}",
        type=STRIPE_EVENT_RECEIVED_TYPE,
        time=time,
        subject=str(data.get("event_type") or "") or None,
        data=dict(data),
    )


async def emit_stripe_event(event: WorkflowEvent) -> None:
    from services.events.dispatch import emit

    await emit(event, wire_routing_key=_WIRE_KEY, broadcast=False)


__all__ = [
    "STRIPE_EVENT_RECEIVED_TYPE",
    "emit_stripe_event",
    "stripe_event_received",
]
