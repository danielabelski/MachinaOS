"""Dodo Payments Receive — fires when DodoPaymentsWebhookSource accepts a
verified webhook delivery."""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field

from services.events import BaseTriggerParams, WebhookTriggerNode, WorkflowEvent
from services.events.envelope import event_type_matches

from ._credentials import WEBHOOK_KEY_FIELD, DodoPaymentsCredential
from ._events import DODO_PAYMENTS_EVENT_RECEIVED_TYPE
from ._source import DodoPaymentsWebhookSource


class DodoPaymentsReceiveParams(BaseTriggerParams):
    event_type_filter: str = Field(
        default="all",
        description=(
            "Dodo event type to match: 'all', an exact type such as 'payment.succeeded', "
            "or a prefix such as 'subscription.*'"
        ),
    )


class DodoPaymentsReceiveOutput(BaseModel):
    event_id: Optional[str] = None
    event_type: Optional[str] = None
    business_id: Optional[str] = None
    timestamp: Optional[str] = None
    payload_type: Optional[str] = None
    data: Optional[dict] = None

    model_config = ConfigDict(extra="allow")


class DodoPaymentsReceiveNode(WebhookTriggerNode):
    type = "dodoPaymentsReceive"
    display_name = "Dodo Payments Receive"
    subtitle = "Webhook Event"
    group = ("payments", "trigger")
    description = "Trigger a workflow when Dodo Payments sends a webhook event (payments, refunds, disputes, subscriptions, ...)"
    component_kind = "trigger"
    handles = ({"name": "output-main", "kind": "output", "position": "right", "label": "Output", "role": "main"},)
    credentials = (DodoPaymentsCredential,)
    webhook_source = DodoPaymentsWebhookSource
    # The canvas Run's waiter key, and the type the source dispatches. Left
    # unset, the base class would derive the source's own type
    # ("dodo_payments.webhook"), which no envelope carries.
    event_type = DODO_PAYMENTS_EVENT_RECEIVED_TYPE
    Params = DodoPaymentsReceiveParams
    Output = DodoPaymentsReceiveOutput

    def build_filter(self, params: DodoPaymentsReceiveParams) -> Callable[[Any], bool]:
        """Match on the shaped event the source emits.

        Both callers hand the predicate the envelope's ``data``: the event
        waiter on a canvas Run (and the Temporal-off deploy path), and
        ``evaluate_trigger_filter_activity`` on a deployed Temporal listener.
        The base filter rebuilds an envelope from what it receives, which
        would raise on every Dodo event and, deployed, fail open. Every Dodo
        event shares one CloudEvents type, so the Dodo type is read from
        ``data["event_type"]``.
        """
        pattern = (params.event_type_filter or "all").strip()

        def matches(event: Any) -> bool:
            data = event.data if isinstance(event, WorkflowEvent) else event
            return isinstance(data, dict) and event_type_matches(str(data.get("event_type") or ""), pattern)

        return matches

    async def _check_precondition(self) -> Optional[str]:
        """Without a key and a webhook secret, every delivery is refused with
        503 and a canvas Run would wait for nothing: say so at once."""
        try:
            secrets = await DodoPaymentsCredential.resolve()
        except PermissionError:
            return "Add the Dodo Payments API key in Credentials -> Dodo Payments, then its webhook secret."
        if not secrets.get(WEBHOOK_KEY_FIELD):
            return (
                "Save the webhook secret (whsec_...) in Credentials -> Dodo Payments: Dodo signs every "
                "delivery to /webhook/dodo-payments with it."
            )
        return None

    def shape_output(self, event: WorkflowEvent) -> Dict:
        """Unwrap the shaped event, which is what the deployed path emits, so
        a canvas Run and a deployed run see the same fields."""
        return event.data if isinstance(event.data, dict) else {}


__all__ = ["DodoPaymentsReceiveNode", "DodoPaymentsReceiveOutput", "DodoPaymentsReceiveParams"]
