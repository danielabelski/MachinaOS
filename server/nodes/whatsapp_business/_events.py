"""CloudEvents factories and the dispatch wrapper for the Cloud API plugin.

Two things here are load-bearing and easy to get wrong.

**The base class does not reach deployed listeners.** ``WebhookSource.handle``
only calls ``event_waiter.dispatch``, which serves the canvas Run path.
Deployed triggers are Temporal ``TriggerListenerWorkflow`` executions reached
by ``services.events.dispatch.emit``. A source that does not call ``emit``
works perfectly when you press Run and does nothing at all once deployed.

**The type strings must match the canary registration exactly.** ``emit``
runs a Visibility query for ``EventType='<type>'``, and the listener carries
whatever ``register_canary_trigger_type`` recorded. A mismatch is silent: the
listener sits Running forever and no signal ever arrives.

Message and status events carry distinct types on purpose. Per-node filters
are not applied on the deployed push path, so the CloudEvents type is the
only discriminator that actually works there.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from services.events.envelope import WorkflowEvent


_SOURCE = "opencompany://nodes/whatsapp_business"

# Inner CloudEvents types. These exact strings are what
# ``register_canary_trigger_type`` records for each node.
MESSAGE_RECEIVED_TYPE = "com.opencompany.whatsapp_business.message.received"
STATUS_UPDATED_TYPE = "com.opencompany.whatsapp_business.status.updated"

# Outer WS wire keys. Distinct from the unofficial plugin's whatsapp_* keys,
# which are already claimed and would raise on registration.
_MESSAGE_WIRE_KEY = "whatsapp_business_message_received"
_STATUS_WIRE_KEY = "whatsapp_business_status_updated"


def message_received(data: Mapping[str, Any], *, event_id: str) -> WorkflowEvent:
    """An inbound WhatsApp message.

    ``event_id`` is the ``wamid``, which Meta guarantees unique per message.
    The listener dedups on it, and Meta retries undelivered webhooks for up
    to seven days, so a stable id is what stops a replay re-running a
    workflow.
    """
    return WorkflowEvent(
        source=_SOURCE,
        type=MESSAGE_RECEIVED_TYPE,
        subject=str(data.get("from") or "") or None,
        data=dict(data),
        id=event_id,
    )


def status_updated(data: Mapping[str, Any], *, event_id: str) -> WorkflowEvent:
    """A delivery-status callback for a message we sent.

    ``event_id`` must be ``<wamid>:<status>``, never the bare wamid. The same
    message legitimately reports sent -> delivered -> read, so deduping on
    the id alone would collapse the whole lifecycle into one event and drop
    two of the three.
    """
    return WorkflowEvent(
        source=_SOURCE,
        type=STATUS_UPDATED_TYPE,
        subject=str(data.get("status") or "") or None,
        data=dict(data),
        id=event_id,
    )


async def emit_message_received(data: Mapping[str, Any], *, event_id: str) -> None:
    from services.events.dispatch import emit

    await emit(message_received(data, event_id=event_id), wire_routing_key=_MESSAGE_WIRE_KEY, broadcast=False)


async def emit_status_updated(data: Mapping[str, Any], *, event_id: str) -> None:
    from services.events.dispatch import emit

    await emit(status_updated(data, event_id=event_id), wire_routing_key=_STATUS_WIRE_KEY, broadcast=False)


__all__ = [
    "MESSAGE_RECEIVED_TYPE",
    "STATUS_UPDATED_TYPE",
    "emit_message_received",
    "emit_status_updated",
    "message_received",
    "status_updated",
]
