"""Contract tests for the generalized event-source framework."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from typing import Iterable
from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.node_contract


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ============================================================================
# WorkflowEvent envelope
# ============================================================================


class TestWorkflowEvent:
    def test_required_fields(self):
        from services.events import WorkflowEvent

        ev = WorkflowEvent(source="stripe://acct_1", type="stripe.charge.succeeded")
        assert ev.specversion == "1.0"
        assert ev.id  # uuid default
        assert ev.datacontenttype == "application/json"

    def test_round_trip_json(self):
        from services.events import WorkflowEvent

        ev = WorkflowEvent(
            source="stripe://acct_1",
            type="stripe.charge.succeeded",
            data={"amount": 1000},
        )
        restored = WorkflowEvent.model_validate(json.loads(ev.model_dump_json()))
        assert restored.type == ev.type
        assert restored.data == {"amount": 1000}
        assert restored.id == ev.id

    def test_matches_type_glob(self):
        from services.events import WorkflowEvent

        ev = WorkflowEvent(source="stripe://x", type="stripe.charge.succeeded")
        assert ev.matches_type("all") is True
        assert ev.matches_type("") is True
        assert ev.matches_type("stripe.charge.succeeded") is True
        assert ev.matches_type("stripe.charge.*") is True
        assert ev.matches_type("stripe.*") is True
        assert ev.matches_type("payment_intent.*") is False

    def test_from_legacy(self):
        from services.events import WorkflowEvent

        ev = WorkflowEvent.from_legacy("whatsapp_message_received", {"text": "hi"})
        assert ev.type == "whatsapp_message_received"
        assert ev.data == {"text": "hi"}
        assert ev.source.startswith("legacy://")

    def test_agent_capability_is_a_safe_cloudevent(self):
        from services.events import WorkflowEvent

        ev = WorkflowEvent.agent_capability(
            "agent-7",
            capability_kind="skill",
            capability_name="write-todos",
            state="loaded",
            workflow_id="7",
            execution_id="12",
            root_execution_id="10",
            target_node_id="master-skill-7",
            tool_call_id="call-1",
            content_hash="sha256-safe",
            event_id="occurrence-1",
        )
        dumped = ev.model_dump(mode="json", exclude_none=True)

        assert dumped["specversion"] == "1.0"
        assert dumped["id"] == "occurrence-1"
        assert dumped["source"] == "opencompany://services/agent"
        assert dumped["type"] == "com.opencompany.agent.skill.loaded"
        assert dumped["subject"] == "agent-7"
        assert dumped["dataschema"].endswith("agent.skill.loaded.json")
        assert dumped["data"]["author_node_id"] == "agent-7"
        # Operational scope belongs to data, not invalid snake_case
        # CloudEvents extension attributes.
        assert "execution_id" not in {key for key in dumped if dumped[key] is not None}
        for secret_field in ("prompt", "tool_args", "result", "instructions", "resource", "error"):
            assert secret_field not in dumped["data"]

    @pytest.mark.parametrize(
        ("kind", "state"),
        [("skill", "started"), ("tool", "loaded"), ("other", "started")],
    )
    def test_agent_capability_rejects_invalid_kind_state_pairs(self, kind, state):
        from services.events import WorkflowEvent

        with pytest.raises(ValueError):
            WorkflowEvent.agent_capability(
                "agent-1",
                capability_kind=kind,
                capability_name="capability",
                state=state,
            )

    def test_capability_broadcaster_keeps_cloudevent_envelope_intact(self):
        from services.status_broadcaster import StatusBroadcaster

        broadcaster = StatusBroadcaster()
        broadcaster.broadcast = AsyncMock()
        event = _run(
            broadcaster.broadcast_agent_capability(
                "agent-1",
                capability_kind="tool",
                capability_name="write_todos",
                state="started",
                workflow_id="1",
                event_id="tool-start-1",
            )
        )

        assert event.subject == "agent-1"
        wire = broadcaster.broadcast.await_args.args[0]
        assert wire["type"] == "agent_capability"
        assert wire["data"]["specversion"] == "1.0"
        assert wire["data"]["id"] == "tool-start-1"
        assert wire["data"]["type"] == "com.opencompany.agent.tool.started"
        assert wire["data"]["data"]["workflow_id"] == "1"


# ============================================================================
# Verifiers
# ============================================================================


class TestStripeVerifier:
    SECRET = "whsec_test"

    def _sign(self, body: bytes, ts: int | None = None) -> dict:
        ts = ts if ts is not None else int(time.time())
        signed = f"{ts}.".encode() + body
        sig = hmac.new(self.SECRET.encode(), signed, hashlib.sha256).hexdigest()
        return {"Stripe-Signature": f"t={ts},v1={sig}"}

    def test_valid_signature_passes(self):
        from services.events import StripeVerifier

        body = b'{"id":"evt_1"}'
        StripeVerifier.verify(self._sign(body), body, self.SECRET)

    def test_tampered_body_rejected(self):
        from services.events import StripeVerifier

        body = b'{"id":"evt_1"}'
        headers = self._sign(body)
        with pytest.raises(ValueError):
            StripeVerifier.verify(headers, b'{"id":"evt_2"}', self.SECRET)

    def test_missing_header_rejected(self):
        from services.events import StripeVerifier

        with pytest.raises(ValueError, match="missing"):
            StripeVerifier.verify({}, b"{}", self.SECRET)

    def test_malformed_header_rejected(self):
        from services.events import StripeVerifier

        with pytest.raises(ValueError):
            StripeVerifier.verify({"Stripe-Signature": "garbage"}, b"{}", self.SECRET)


class TestGitHubVerifier:
    def test_round_trip(self):
        from services.events import GitHubVerifier

        secret = "shh"
        body = b'{"action":"opened"}'
        sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        GitHubVerifier.verify({"X-Hub-Signature-256": sig}, body, secret)

    def test_tampered_rejected(self):
        from services.events import GitHubVerifier

        with pytest.raises(ValueError):
            GitHubVerifier.verify({"X-Hub-Signature-256": "sha256=deadbeef"}, b"{}", "shh")


class TestStandardWebhooksVerifier:
    SECRET_RAW = b"super-secret-key-bytes"
    SECRET = "whsec_" + base64.b64encode(SECRET_RAW).decode()

    def _signed(self, body: bytes, ts: int, msg_id: str = "msg_abc") -> dict:
        signed = f"{msg_id}.{ts}.".encode() + body
        sig = base64.b64encode(hmac.new(self.SECRET_RAW, signed, hashlib.sha256).digest()).decode()
        return {
            "webhook-id": msg_id,
            "webhook-timestamp": str(ts),
            "webhook-signature": f"v1,{sig}",
        }

    def test_round_trip(self):
        from services.events import StandardWebhooksVerifier

        body = b'{"foo":"bar"}'
        StandardWebhooksVerifier.verify(self._signed(body, int(time.time())), body, self.SECRET)

    def test_rotated_secret_list_accepts_any_matching_entry(self):
        from services.events import StandardWebhooksVerifier

        body = b'{"foo":"bar"}'
        headers = self._signed(body, int(time.time()))
        headers["webhook-signature"] = "v1,c3RhbGU= " + headers["webhook-signature"]
        StandardWebhooksVerifier.verify(headers, body, self.SECRET)

    def test_tampered_rejected(self):
        from services.events import StandardWebhooksVerifier

        headers = self._signed(b'{"foo":"bar"}', int(time.time()))
        with pytest.raises(ValueError, match="signature mismatch"):
            StandardWebhooksVerifier.verify(headers, b'{"foo":"baz"}', self.SECRET)

    @pytest.mark.parametrize("offset", [-301, 301])
    def test_timestamp_outside_the_window_is_rejected(self, offset):
        """A correctly signed delivery replayed later (or stamped far ahead)
        fails: the spec bounds the timestamp to five minutes either way."""
        from services.events import StandardWebhooksVerifier

        body = b'{"foo":"bar"}'
        headers = self._signed(body, int(time.time()) + offset)
        with pytest.raises(ValueError, match="tolerance"):
            StandardWebhooksVerifier.verify(headers, body, self.SECRET)

    def test_timestamp_inside_the_window_is_accepted(self):
        from services.events import StandardWebhooksVerifier

        body = b'{"foo":"bar"}'
        StandardWebhooksVerifier.verify(self._signed(body, int(time.time()) - 240), body, self.SECRET)

    def test_non_numeric_timestamp_rejected(self):
        from services.events import StandardWebhooksVerifier

        headers = self._signed(b"{}", int(time.time()))
        headers["webhook-timestamp"] = "yesterday"
        with pytest.raises(ValueError, match="not a number"):
            StandardWebhooksVerifier.verify(headers, b"{}", self.SECRET)

    def test_missing_headers_rejected(self):
        from services.events import StandardWebhooksVerifier

        with pytest.raises(ValueError, match="missing"):
            StandardWebhooksVerifier.verify({}, b"{}", self.SECRET)


class TestHmacVerifier:
    def test_round_trip(self):
        from services.events import HmacVerifier

        secret = "shh"
        body = b'{"x":1}'
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        HmacVerifier.verify({"X-Signature-256": sig}, body, secret)


# ============================================================================
# PollingEventSource loop
# ============================================================================


class TestPollingEventSource:
    def test_emits_what_poll_once_returns_then_sleeps(self):
        from services.events import PollingEventSource, WorkflowEvent

        class FakePolling(PollingEventSource):
            type = "fake.polling"
            poll_interval_default = 0  # tight loop for the test

            def __init__(self):
                super().__init__()
                self.calls = 0

            async def poll_once(self, state) -> Iterable[WorkflowEvent]:
                self.calls += 1
                if self.calls == 1:
                    return [WorkflowEvent(source="x", type="fake.tick", data={"n": 1})]
                self._stopped = True
                return []

        async def drain():
            src = FakePolling()
            seen = []
            async for ev in src.emit():
                seen.append(ev)
                if len(seen) >= 1:
                    src._stopped = True
            return seen

        events = _run(drain())
        assert len(events) == 1
        assert events[0].type == "fake.tick"


# ============================================================================
# WebhookSource — handle() integration
# ============================================================================


class TestWebhookSourceHandle:
    def _build_source(self, secret_value: str | None):
        from services.events import StripeVerifier, WebhookSource, WorkflowEvent

        class _Cred:
            @classmethod
            async def resolve(cls):
                if secret_value is None:
                    raise PermissionError
                return {"api_key": "sk_test", "test_secret": secret_value}

        class FakeSource(WebhookSource):
            type = "fake.hook"
            path = "fake"
            verifier = StripeVerifier
            secret_field = "test_secret"
            credential = _Cred

            async def shape(self, request, body, payload):
                return WorkflowEvent(source="fake://x", type="fake.event", data=payload)

        return FakeSource()

    def _signed_request(self, body: bytes, secret: str):
        ts = int(time.time())
        signed = f"{ts}.".encode() + body
        sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()

        class FakeReq:
            headers = {"Stripe-Signature": f"t={ts},v1={sig}"}

            async def body(self):
                return body

            async def stream(self):
                yield body

        return FakeReq()

    def test_valid_signature_dispatches(self):
        src = self._build_source(secret_value="whsec_test")
        body = b'{"event":"payload"}'
        req = self._signed_request(body, "whsec_test")
        with patch("services.event_waiter.dispatch") as dispatch:
            ev = _run(src.handle(req))
        assert ev.type == "fake.event"
        dispatch.assert_called_once()
        # The envelope is passed alone. The two-argument form means
        # ``(event_type: str, data: Dict)``, so passing the event as the
        # second argument made ``data`` the envelope itself -- see
        # test_dispatch_with_live_waiter_does_not_crash below.
        assert dispatch.call_args[0] == (ev,)

    def test_dispatch_with_live_waiter_does_not_crash(self):
        """Regression: the dispatcher must receive an unpackable envelope.

        ``handle`` used to call ``dispatch(self.type, event)``. The
        two-argument form is ``(event_type, data)``, so ``data`` became the
        WorkflowEvent and every ``data.get(...)`` inside the dispatcher
        raised AttributeError -- but only once a waiter was actually
        registered, which is why no existing test caught it. This registers
        a real waiter and dispatches for real.
        """
        import asyncio as _asyncio

        from services import event_waiter

        src = self._build_source(secret_value="whsec_test")
        body = b'{"event":"payload","from_id":"alice"}'
        req = self._signed_request(body, "whsec_test")

        async def _scenario():
            # Built inline rather than via event_waiter.register(), which
            # requires a TRIGGER_REGISTRY entry this fake type has no
            # business adding. The future must be created on the loop that
            # runs handle(), so it cannot be set up outside the coroutine.
            waiter = event_waiter.Waiter(
                node_id="n1",
                node_type="fakeTrigger",
                event_type="fake.event",
                filter_fn=lambda _data: True,
                future=_asyncio.get_running_loop().create_future(),
            )
            event_waiter._waiters[waiter.id] = waiter
            try:
                event = await src.handle(req)
                return event, waiter.future
            finally:
                event_waiter.cancel_for_node("n1")

        ev, future = _run(_scenario())
        assert future.done()
        # The waiter resolves with the envelope's *data*, not the envelope.
        assert future.result() == ev.data
        assert future.result()["from_id"] == "alice"

    def test_tampered_signature_raises_400(self):
        from fastapi import HTTPException

        src = self._build_source(secret_value="whsec_test")
        body = b'{"event":"payload"}'
        req = self._signed_request(body, "whsec_other")  # signed with different secret
        with pytest.raises(HTTPException) as exc:
            _run(src.handle(req))
        assert exc.value.status_code == 400

    def test_missing_secret_rejects_503(self):
        """Credential resolution fails -> fail closed with 503 + Retry-After."""
        from fastapi import HTTPException

        src = self._build_source(secret_value=None)
        body = b'{"event":"payload"}'

        class FakeReq:
            headers = {}

            async def body(self):
                return body

            async def stream(self):
                yield body

        with patch("services.event_waiter.dispatch") as dispatch:
            with pytest.raises(HTTPException) as exc:
                _run(src.handle(FakeReq()))
        assert exc.value.status_code == 503
        assert exc.value.headers.get("Retry-After") == "5"
        dispatch.assert_not_called()

    def test_secret_field_absent_rejects_503(self):
        """Credential resolves but lacks the secret field -> fail closed with 503."""
        from fastapi import HTTPException
        from services.events import StripeVerifier, WebhookSource, WorkflowEvent

        class _Cred:
            @classmethod
            async def resolve(cls):
                return {"api_key": "sk_test"}  # secret_field key missing

        class FakeSource(WebhookSource):
            type = "fake.hook"
            path = "fake"
            verifier = StripeVerifier
            secret_field = "test_secret"
            credential = _Cred

            async def shape(self, request, body, payload):
                return WorkflowEvent(source="fake://x", type="fake.event", data=payload)

        src = FakeSource()
        body = b'{"event":"payload"}'

        class FakeReq:
            headers = {}

            async def body(self):
                return body

            async def stream(self):
                yield body

        with patch("services.event_waiter.dispatch") as dispatch:
            with pytest.raises(HTTPException) as exc:
                _run(src.handle(FakeReq()))
        assert exc.value.status_code == 503
        dispatch.assert_not_called()


# ============================================================================
# WebhookSource.handle_get — provider subscription handshakes
# ============================================================================


class TestWebhookSourceHandleGet:
    """A source may answer a GET handshake with its own Response body.

    Providers verify webhook ownership before delivering anything, and the
    expected reply is provider-specific (Meta echoes ``hub.challenge`` as
    text/plain). The router's fixed JSON body cannot express that, so the
    source owns the response.
    """

    def test_default_declines(self):
        """The base implementation returns None so existing sources are
        untouched -- the router then falls through to handle()."""
        from services.events import WebhookSource

        class Bare(WebhookSource):
            type = "bare.hook"
            path = "bare"

        class FakeReq:
            method = "GET"
            headers = {}
            query_params = {}

        assert _run(Bare().handle_get(FakeReq())) is None

    def test_override_can_return_plain_text(self):
        """A source that overrides it controls status, body and media type."""
        from fastapi.responses import PlainTextResponse

        from services.events import WebhookSource

        class Echoing(WebhookSource):
            type = "echo.hook"
            path = "echo"

            async def handle_get(self, request):
                challenge = request.query_params.get("hub.challenge")
                if challenge is None:
                    return None
                return PlainTextResponse(challenge)

        class FakeReq:
            method = "GET"
            headers = {}
            query_params = {"hub.challenge": "1158201444"}

        resp = _run(Echoing().handle_get(FakeReq()))
        assert resp.status_code == 200
        # Meta requires the bare challenge value, not a JSON wrapper.
        assert resp.body == b"1158201444"
        assert resp.media_type == "text/plain"

    def test_override_may_still_decline(self):
        """Returning None from an override falls through to handle()."""
        from fastapi.responses import PlainTextResponse

        from services.events import WebhookSource

        class Echoing(WebhookSource):
            type = "echo.hook"
            path = "echo"

            async def handle_get(self, request):
                challenge = request.query_params.get("hub.challenge")
                return PlainTextResponse(challenge) if challenge else None

        class FakeReq:
            method = "GET"
            headers = {}
            query_params = {}

        assert _run(Echoing().handle_get(FakeReq())) is None


# ============================================================================
# DaemonEventSource lifecycle (mocked ProcessService)
# ============================================================================


class TestDaemonEventSource:
    def test_start_calls_process_service_start(self):
        from services.events import DaemonEventSource

        class FakeDaemon(DaemonEventSource):
            type = "fake.daemon"
            process_name = "fake-daemon"
            binary_name = "echo"  # always on PATH

            def build_command(self, secrets):
                return "echo hello"

            def parse_line(self, stream, line):
                return None

        async def go():
            with (
                patch("services.events.daemon.shutil.which", return_value="/usr/bin/echo"),
                patch("services.events.daemon.get_process_service") as get_ps,
            ):
                ps = get_ps.return_value

                async def fake_start(**kwargs):
                    return {"success": True, "result": {"pid": 4242}}

                ps.start.side_effect = fake_start

                async def fake_stop(**kwargs):
                    return {"success": True}

                ps.stop.side_effect = fake_stop

                src = FakeDaemon()
                result = await src.start()
                assert result["success"] is True
                assert src.pid == 4242
                ps.start.assert_called_once()
                # Cancel the tail task so the test exits cleanly.
                await src.stop()
                assert src.pid is None

        _run(go())

    def test_start_fails_when_binary_missing(self):
        from services.events import DaemonEventSource

        class FakeDaemon(DaemonEventSource):
            type = "fake.daemon"
            process_name = "fake-daemon"
            binary_name = "definitely-not-on-path-zzzzz"
            install_hint = "see docs"

            def build_command(self, secrets):
                return "x"

        async def go():
            with patch("services.events.daemon.shutil.which", return_value=None):
                src = FakeDaemon()
                result = await src.start()
                assert result["success"] is False
                assert "PATH" in result["error"]
                assert "see docs" in result["error"]

        _run(go())
