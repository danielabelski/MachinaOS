"""Dodo Payments credential — an API key kept in OpenCompany's encrypted
credential store (``ApiKeyCredential`` shape), plus two companion rows.

Three rows under one provider:

* ``apiKey`` (stored under the provider id ``dodo_payments``) — the API key,
  the field the credentials modal validates;
* ``dodo_payments_mode`` — ``test_mode`` or ``live_mode``, written by
  validation, never typed by the owner;
* ``dodo_payments_webhook_key`` — the ``whsec_`` signing secret of the Dodo
  webhook endpoint that posts to OpenCompany, read by Dodo Payments Receive.

A Dodo key works only in the mode it was created in and says nothing about
which (https://docs.dodopayments.com/miscellaneous/test-mode-vs-live-mode).
Validation asks each mode in turn, test first, and saves the one that
accepted the key; every call then goes to that mode's base URL, so a node
never reaches live payments with a key the owner made for testing. The key
is always passed to the SDK explicitly, so a ``DODO_PAYMENTS_API_KEY`` in the
server environment never authenticates a node.

This module stays free of the SDK: the plugin registers and the modal
answers even when ``dodopayments`` is missing.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping

from services.plugin.credential import ApiKeyCredential, ProbeResult

MODE_FIELD = "dodo_payments_mode"
WEBHOOK_KEY_FIELD = "dodo_payments_webhook_key"
# The SDK's environment names, in the order validation asks them.
MODES = ("test_mode", "live_mode")


class DodoPaymentsCredential(ApiKeyCredential):
    id = "dodo_payments"
    display_name = "Dodo Payments"
    category = "Payments"
    docs_url = "https://docs.dodopayments.com/api-reference/introduction"
    key_location = "bearer"
    extra_fields = (MODE_FIELD, WEBHOOK_KEY_FIELD)

    @classmethod
    async def validate(cls, data: Dict[str, Any]) -> Dict[str, Any]:
        """The base scaffold, then the mode the probe found, saved beside the
        key it belongs to."""
        response = await super().validate(data)
        if response.get("valid"):
            from services.plugin.deps import get_auth_service

            await get_auth_service().store_api_key(
                MODE_FIELD, response["mode"], models=[], session_id=data.get("session_id", "default")
            )
        return response

    @classmethod
    async def _probe(cls, api_key: str) -> ProbeResult:
        """Find the mode the key belongs to (``_sdk.key_mode``). Stores
        nothing; :meth:`validate` saves the key and its mode."""
        from services.plugin import NodeUserError

        from ._sdk import key_mode

        try:
            mode = await key_mode(api_key, timeout=cls.probe_timeout_seconds)
        except NodeUserError as exc:
            return ProbeResult(valid=False, message=str(exc))
        return ProbeResult(
            valid=True,
            message=f"Dodo Payments API key is valid ({mode.replace('_', ' ')})",
            extra={"mode": mode},
        )


def stored_mode(secrets: Mapping[str, Any]) -> str:
    """The mode saved when the key was validated. Missing, it raises the same
    annotated ``PermissionError`` as a missing key, so the node answers with
    the ``PermissionDeniedError`` envelope that points at the modal."""
    mode = secrets.get(MODE_FIELD)
    if mode not in MODES:
        err = PermissionError(
            "Validate the Dodo Payments API key in Credentials -> Dodo Payments: "
            "validation finds whether it is a test or a live key."
        )
        err.provider = DodoPaymentsCredential.id
        err.reason = "missing"
        err.auth = DodoPaymentsCredential.auth
        raise err
    return mode
