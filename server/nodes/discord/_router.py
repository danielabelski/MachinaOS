"""HTTP surface: the interactions endpoint and the OAuth2 callback.

A plugin-owned router rather than a ``WebhookSource``, for three reasons the
generic intake cannot express:

* Discord's endpoint-validation probe deliberately sends bad signatures and
  requires **401**. ``WebhookSource.handle`` raises 400 on verifier failure,
  and a 400 makes Discord refuse to save the endpoint URL at all.
* The initial response is due within three seconds. ``WebhookSource.handle``
  awaits shaping, ``emit`` (a Temporal Visibility query plus a Signal per
  consumer) and dispatch before the router writes a byte. That is not a
  latency budget a plugin controls. This ACKs first and fans out after.
* The reply body is part of the protocol -- PING must be answered with
  ``{"type": 1}`` -- while the generic route returns a fixed envelope.

Interaction delivery is mutually exclusive with the gateway: setting an
endpoint URL stops INTERACTION_CREATE arriving over the socket. The account's
public key is what identifies which app is calling, so each app points at its
own path rather than the server trial-verifying against every stored key,
which would be both a timing oracle and an availability multiplier.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from core.logging import get_logger
from services.events.oauth_lifecycle import make_oauth_callback_router
from services.events.webhook import read_body_capped

from . import _interactions
from ._accounts import DEFAULT_ACCOUNT, PUBLIC_KEY_KEY, storage_scope
from ._oauth import build_oauth
from ._verifier import DiscordEd25519Verifier

logger = get_logger(__name__)


def _user_info_to_email(info: Dict[str, Any]) -> str:
    # /users/@me returns a username; email would need the email scope, which
    # is deliberately not requested.
    return info.get("email") or info.get("username") or "Unknown"


# One router, built by the OAuth factory and then extended, rather than a
# fresh APIRouter with the factory's router included into it. On an APIRouter
# (unlike an app) include_router leaves a pathless placeholder instead of
# flattening the child's routes: the callback never mounts, and the pathless
# entry reads as an ungated route to the public-surface invariant.
#
# The factory sets prefix="/api/discord", so paths added below are relative
# to it.
router = make_oauth_callback_router(
    provider="discord",
    oauth_factory=build_oauth,
    user_info_to_email=_user_info_to_email,
    color_hex="#5865F2",
)

# Strong references to in-flight fan-out tasks. The event loop only holds a
# weak reference, so without this a task can be garbage-collected mid-await
# and the interaction silently never reaches the workflow.
_PENDING: set[asyncio.Task] = set()


async def _public_key(account_id: str) -> str:
    from services.plugin.deps import get_auth_service

    return await get_auth_service().get_api_key(PUBLIC_KEY_KEY, storage_scope(account_id)) or ""


async def _handle_interaction(request: Request, account_id: str) -> Response:
    # The signature covers the exact bytes Discord sent. Re-serialising the
    # parsed JSON would change key order or separators and break verification
    # for authentic requests, so the body is read raw and parsed afterwards.
    body = await read_body_capped(request)

    public_key = await _public_key(account_id)
    if not public_key:
        # Fail closed. Without a key nothing can be authenticated, and
        # accepting anyway would let anyone trigger workflows.
        return JSONResponse(
            {"error": "Discord public key is not configured for this account."},
            status_code=503,
            headers={"Retry-After": "5"},
        )

    try:
        DiscordEd25519Verifier.verify(dict(request.headers), body, public_key)
    except ValueError as exc:
        # 401, not 400: Discord's endpoint-validation probe sends a bad
        # signature on purpose and refuses to save the URL unless it gets a
        # 401 back.
        logger.debug("discord interaction signature rejected", error=str(exc))
        return JSONResponse({"error": "invalid request signature"}, status_code=401)

    try:
        payload: Dict[str, Any] = await request.json()
    except Exception:
        return JSONResponse({"error": "malformed interaction payload"}, status_code=400)

    interaction_type = payload.get("type")

    if interaction_type == _interactions.PING:
        return JSONResponse({"type": _interactions.PONG})

    # Acknowledge inside the three-second window, then do the work. The
    # workflow finishes the interaction later through discordAction.
    event = _interactions.shape_interaction(payload, account_id=account_id)
    task = asyncio.create_task(_emit(event))
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)

    return JSONResponse({"type": _interactions.deferred_response_type(interaction_type)})


async def _emit(event: Dict[str, Any]) -> None:
    from ._events import dispatch_discord_interaction_created

    try:
        await dispatch_discord_interaction_created(event)
    except Exception as exc:
        # The HTTP reply already went out; losing the fan-out must not
        # surface as an unhandled task exception.
        logger.warning("discord interaction dispatch failed", error=str(exc))


@router.post("/interactions")
async def interactions_default(request: Request) -> Response:
    return await _handle_interaction(request, DEFAULT_ACCOUNT)


@router.post("/interactions/{account_id}")
async def interactions_for_account(request: Request, account_id: str) -> Response:
    return await _handle_interaction(request, account_id)


__all__ = ["router"]
