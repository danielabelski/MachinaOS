"""WebhookSource — push source backed by an HTTP POST to /webhook/{path}.

Plugins subclass and declare:

    class MyWebhook(WebhookSource):
        path = "myprovider"
        verifier = MyVerifier        # WebhookVerifier subclass (optional)
        secret_field = "my_webhook_secret"  # extra field on the credential

        async def shape(self, request, body, payload) -> WorkflowEvent: ...

A single edit to ``routers/webhook.py`` consults :data:`WEBHOOK_SOURCES`
before the legacy generic dispatch path. No plugin name is hardcoded
in the router.
"""

from __future__ import annotations

import json
from typing import ClassVar, Dict, Optional, Type

from fastapi import HTTPException, Request, Response

from core.logging import get_logger

from .envelope import WorkflowEvent
from .push import PushEventSource
from .verifiers import WebhookVerifier

logger = get_logger(__name__)


WEBHOOK_SOURCES: Dict[str, "WebhookSource"] = {}


async def read_body_capped(request: Request) -> bytes:
    """Read a webhook's body, refusing it with 413 past
    ``WEBHOOK_MAX_BODY_BYTES`` (default in ``.env.template``).

    The declared ``Content-Length`` refuses an honest oversized request
    before any of it is read; the running total is what enforces the
    limit, because the header is the caller's to set.
    """
    from core.env_defaults import env_int

    limit = env_int("WEBHOOK_MAX_BODY_BYTES")
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise _body_too_large(limit)
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise _body_too_large(limit)
        chunks.append(chunk)
    body = b"".join(chunks)
    # Starlette's body cache: request.body() and request.json() now return
    # these bytes instead of reading the consumed stream.
    request._body = body
    return body


def _body_too_large(limit: int) -> HTTPException:
    return HTTPException(status_code=413, detail=f"Request body is larger than the {limit}-byte webhook limit.")


def register_webhook_source(source: "WebhookSource") -> None:
    """Idempotent: same instance for the same path is a no-op; conflicts raise."""
    existing = WEBHOOK_SOURCES.get(source.path)
    if existing is not None and existing is not source:
        raise ValueError(f"Webhook path {source.path!r} already registered to {type(existing).__name__}")
    WEBHOOK_SOURCES[source.path] = source


class WebhookSource(PushEventSource):
    """Base for HTTP-webhook event sources.

    Subclass contract:
        path:           URL fragment under /webhook/
        verifier:       WebhookVerifier subclass (or None to skip verification)
        secret_field:   credential extra-field name holding the signing secret
        shape():        turn the verified request into a WorkflowEvent
    """

    path: ClassVar[str] = ""
    verifier: ClassVar[Optional[Type[WebhookVerifier]]] = None
    secret_field: ClassVar[Optional[str]] = None

    async def _resolve_secret(self) -> Optional[str]:
        if self.secret_field is None or self.credential is None:
            return None
        try:
            secrets = await self.credential.resolve()
        except PermissionError:
            return None
        return secrets.get(self.secret_field)

    async def shape(self, request: Request, body: bytes, payload: dict) -> WorkflowEvent:
        """Override to map the verified payload into a CloudEvent."""
        return WorkflowEvent(
            source=f"webhook://{self.path}",
            type=f"webhook.{self.path}",
            data=payload,
        )

    async def handle_get(self, request: Request) -> Optional[Response]:
        """Answer a provider's subscription handshake.

        Several providers verify webhook ownership with a ``GET`` before
        they will deliver anything: Meta echoes ``hub.challenge`` as
        ``text/plain``, and others use their own challenge shapes. Those
        responses are not the router's fixed JSON body, so the source has
        to own them.

        Return ``None`` (the default) to decline, in which case the router
        falls through to the normal :meth:`handle` path — so every source
        that does not override this behaves exactly as before.
        """
        return None

    async def handle(self, request: Request) -> WorkflowEvent:
        """Called by the webhook router. Verifies signature, parses the
        body, shapes the event, dispatches into ``event_waiter``, and
        returns the event for logging / response shaping. Subclasses that
        feed deployed triggers emit the returned event themselves."""
        body = await read_body_capped(request)
        if self.verifier is not None:
            secret = await self._resolve_secret()
            if not secret:
                # Fail closed: a declared verifier with no secret means we
                # cannot authenticate the sender — reject instead of
                # accepting an unverifiable event.
                logger.warning(
                    "[%s] no signing secret available; rejecting event (503)",
                    self.type or self.path,
                )
                raise HTTPException(
                    status_code=503,
                    detail=f"webhook signing secret for {self.path!r} unavailable; retry later",
                    headers={"Retry-After": "5"},
                )
            try:
                self.verifier.verify(dict(request.headers), body, secret)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))

        try:
            payload = json.loads(body.decode() or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise HTTPException(status_code=400, detail=f"invalid JSON body: {e}")

        event = await self.shape(request, body, payload)

        from services import event_waiter

        # Pass the envelope alone. The two-argument form is
        # ``(event_type: str, data: Dict)``, so handing a WorkflowEvent as
        # the second argument made ``data`` the envelope itself and every
        # ``data.get(...)`` in the dispatcher raised AttributeError as soon
        # as a waiter was active. ``_unpack_event`` already unwraps a
        # WorkflowEvent into ``(type, data)`` on its own.
        event_waiter.dispatch(event)
        return event
