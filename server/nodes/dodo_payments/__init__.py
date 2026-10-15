"""Plugins for the 'payments' palette group — Dodo Payments via the official
dodopayments SDK.

``dodoPaymentsAction`` (AI tool ``dodo_payments_action``): typed payment,
subscription, customer, checkout and refund operations, ``call`` for any SDK
endpoint, and ``list_operations`` to discover them. Auth is the API key saved
in Credentials -> Dodo Payments, whose mode (test or live) validation finds.
The SDK is imported lazily (``_sdk.py``), so the plugin registers and the
credentials modal answers even without it.
"""

from __future__ import annotations

from services.node_output_schemas import register_output_schema
from services.ws_handler_registry import register_option_loader

from ._credentials import DodoPaymentsCredential
from ._sdk import load_dodo_payments_endpoints
from .dodo_payments_action import DodoPaymentsActionNode, DodoPaymentsActionOutput

register_output_schema("dodoPaymentsAction", DodoPaymentsActionOutput)
register_option_loader("dodoPaymentsEndpoints", load_dodo_payments_endpoints)

__all__ = [
    "DodoPaymentsActionNode",
    "DodoPaymentsCredential",
]
