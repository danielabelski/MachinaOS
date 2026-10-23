"""CloudEvents factory + emit wrapper for webhook_trigger.

Per RFC plugin_authoring_rfc.md §6.4: plugin-specific factories live in
the plugin folder.

Delivery: ``_routes.deliver_webhook`` calls :func:`emit_webhook_received`
once per workflow that declares the path. The envelope is scoped to that
workflow, so ``dispatch.emit`` signals only its consumers, and it is never
broadcast: it carries the caller's headers and body, and no client reads
it. webhookTrigger is canary-registered (see
``nodes/trigger/webhook_trigger/__init__.py``) so the deployment manager
skips ``setup_event_trigger`` and the legacy
``broadcaster.send_custom_event`` path (which dispatched to
``event_waiter``) has zero consumers — removed.
"""

from __future__ import annotations

from typing import Any, Mapping

from services.events.envelope import WorkflowEvent


# Outer wire-routing key. Matches ``WebhookTriggerNode.event_type`` and
# the FE WS channel; the inner envelope carries
# ``com.opencompany.webhook.received``.
_WIRE_ROUTING_KEY = "webhook_received"


def webhook_received(webhook_data: Mapping[str, Any]) -> WorkflowEvent:
    """Incoming webhook envelope. ``subject`` is the path so consumers
    can route per-endpoint."""
    payload = dict(webhook_data)
    path = payload.get("path")
    return WorkflowEvent(
        source="opencompany://nodes/webhook_trigger",
        type="com.opencompany.webhook.received",
        subject=str(path) if path else None,
        data=payload,
    )


async def emit_webhook_received(webhook_data: Mapping[str, Any], *, workflow_id: str) -> None:
    """Deliver an incoming webhook to the triggers of ``workflow_id``."""
    from services.events.dispatch import emit

    event = webhook_received(dict(webhook_data))
    event.workflow_id = workflow_id
    await emit(event, wire_routing_key=_WIRE_ROUTING_KEY, broadcast=False)


__all__ = [
    "emit_webhook_received",
    "webhook_received",
]
