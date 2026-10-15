"""Plugins for the 'payments' palette group — Dodo Payments via the official
dodopayments SDK.

* ``dodoPaymentsAction`` (AI tool ``dodo_payments_action``): typed payment,
  subscription, customer, checkout and refund operations, ``call`` for any
  SDK endpoint, and ``list_operations`` to discover them.
* ``dodoPaymentsReceive``: fires on each verified webhook delivery to
  ``/webhook/dodo-payments``, on a canvas Run and deployed.

Auth is the API key saved in Credentials -> Dodo Payments, whose mode (test
or live) validation finds, plus the webhook signing secret for the trigger.
The SDK is imported lazily (``_sdk.py``), so the plugin registers and the
credentials modal answers even without it.
"""

from __future__ import annotations

from services.deployment.canary_registry import register_canary_trigger_type
from services.events import register_webhook_source
from services.node_output_schemas import register_output_schema
from services.ws_handler_registry import register_option_loader

from ._credentials import DodoPaymentsCredential
from ._events import DODO_PAYMENTS_EVENT_RECEIVED_TYPE
from ._sdk import load_dodo_payments_endpoints
from ._source import DodoPaymentsWebhookSource, get_webhook_source
from .dodo_payments_action import DodoPaymentsActionNode, DodoPaymentsActionOutput
from .dodo_payments_receive import DodoPaymentsReceiveNode, DodoPaymentsReceiveOutput

register_webhook_source(get_webhook_source())
# Opt the trigger into the Temporal listener path. The second argument must
# equal the type on every envelope the source emits: it becomes the EventType
# the Visibility query matches, and a mismatch is silent.
register_canary_trigger_type(DodoPaymentsReceiveNode.type, DODO_PAYMENTS_EVENT_RECEIVED_TYPE)
register_output_schema("dodoPaymentsAction", DodoPaymentsActionOutput)
register_output_schema("dodoPaymentsReceive", DodoPaymentsReceiveOutput)
register_option_loader("dodoPaymentsEndpoints", load_dodo_payments_endpoints)

__all__ = [
    "DodoPaymentsActionNode",
    "DodoPaymentsCredential",
    "DodoPaymentsReceiveNode",
    "DodoPaymentsWebhookSource",
    "get_webhook_source",
]
