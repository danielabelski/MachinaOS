"""Scraper credentials (Wave 11.E.1 — per-domain).

``ApifyCredential`` probes through the Apify SDK; ``TikHubCredential`` uses
the declarative httpx probe on purpose — the credentials modal must keep
working even if the ``tikhub`` SDK import fails, so this module stays
SDK-free.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import httpx

from services.plugin.credential import ApiKeyCredential, ProbeResult


class ApifyCredential(ApiKeyCredential):
    id = "apify"
    display_name = "Apify"
    category = "Scrapers"
    key_name = "Authorization"
    key_location = "bearer"
    docs_url = "https://docs.apify.com/api/v2"

    @classmethod
    async def _probe(cls, api_key: str) -> ProbeResult:
        """Probe Apify ``/users/me`` to verify the token + capture
        username / email / plan for display in the credentials panel.

        ``validate_apify_token`` lives on the plugin module
        (``apify_actor.py``) and uses the official ``apify_client`` SDK.
        It returns a dict; we translate to :class:`ProbeResult` so the
        base ``Credential.validate`` handles storage / broadcast.
        """
        from .apify_actor import validate_apify_token

        result = await validate_apify_token(api_key)
        if not result.get("valid"):
            return ProbeResult(
                valid=False,
                message=result.get("error", "Invalid API token"),
            )
        return ProbeResult(
            valid=True,
            message=f"Apify token validated — user: {result.get('username', 'unknown')}",
            extra={
                "username": result.get("username"),
                "email": result.get("email"),
                "plan": result.get("plan"),
            },
        )


_ENVELOPE_MESSAGE_KEYS = ("message", "error", "detail", "msg")


def _envelope_message(body: Dict[str, Any], fallback: str) -> str:
    for key in _ENVELOPE_MESSAGE_KEYS:
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    return fallback


class TikHubCredential(ApiKeyCredential):
    """TikHub (api.tikhub.io) pay-per-request social scraping API.

    The probe hits the authenticated user-info route rather than
    ``/api/v1/health/check`` (which answers without a key). A 401 surfaces
    through the base ``raise_for_status`` path; a 2xx is inspected for the
    in-body ``code`` TikHub uses for API-level failures.
    """

    id = "tikhub"
    display_name = "TikHub"
    category = "Scrapers"
    key_name = "Authorization"
    key_location = "bearer"
    docs_url = "https://docs.tikhub.io"
    probe_url = "https://api.tikhub.io/api/v1/tikhub/user/get_user_info"

    @classmethod
    def _handle_probe_response(cls, response: httpx.Response) -> ProbeResult:
        try:
            body: Any = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return ProbeResult(valid=True, message="TikHub key validated")

        code = body.get("code")
        if isinstance(code, int) and not isinstance(code, bool) and code >= 400:
            return ProbeResult(
                valid=False,
                message=f"TikHub rejected the API key: {_envelope_message(body, f'code {code}')}",
            )

        # Field names come from the CLI's `user info` output; the envelope
        # may carry them at the top level or under ``data``. Tolerate both
        # and tolerate absence — a missing field must not fail validation.
        data = body.get("data") if isinstance(body.get("data"), dict) else {}

        def section(name: str) -> Dict[str, Any]:
            for source in (body, data):
                value = source.get(name)
                if isinstance(value, dict):
                    return value
            return {}

        user = section("user_data")
        key_data = section("api_key_data")
        email: Optional[str] = user.get("email")
        balance = user.get("balance")
        free_credit = user.get("free_credit")
        api_key_name = key_data.get("api_key_name")

        who = email or api_key_name or "account"
        summary = f"TikHub key validated — {who}"
        if balance is not None:
            summary += f" (balance ${balance})"
        return ProbeResult(
            valid=True,
            message=summary,
            extra={
                "email": email,
                "balance": balance,
                "free_credit": free_credit,
                "api_key_name": api_key_name,
            },
        )
