"""Contract tests for the Stripe plugin (Wave 12 framework version).

The plugin is now a thin specialisation of ``services.events`` —
``StripeListenSource`` (DaemonEventSource) supervises ``stripe listen``
and ``StripeWebhookSource`` (WebhookSource) receives forwarded events.
These tests verify the plugin wiring and the receive-node reshape;
end-to-end smoke against the real CLI requires the binary on PATH.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.node_contract


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _signed(body: bytes, secret: str) -> dict:
    ts = int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return {"Stripe-Signature": f"t={ts},v1={sig}"}


def _fake_request(body: bytes, headers: dict | None = None):
    """Return an object with the attributes WebhookSource.handle reads."""

    class R:
        pass

    r = R()
    r.headers = headers or {}

    async def _body():
        return body

    async def _stream():
        yield body

    r.body = _body
    r.stream = _stream
    return r


# ============================================================================
# StripeWebhookSource — shape() and end-to-end handle()
# ============================================================================


class TestStripeWebhookShape:
    SECRET = "whsec_test_xyz"

    def _stub_secret_resolution(self):
        return patch(
            "nodes.stripe._source.StripeCredential.resolve",
            AsyncMock(return_value={"api_key": "sk_test", "stripe_webhook_secret": self.SECRET}),
        )

    def test_shape_extracts_stripe_fields(self):
        from nodes.stripe._events import STRIPE_EVENT_RECEIVED_TYPE
        from nodes.stripe._source import get_webhook_source

        src = get_webhook_source()
        payload = {
            "id": "evt_1",
            "type": "charge.succeeded",
            "created": 1700000000,
            "livemode": False,
            "api_version": "2024-04-10",
            "request": {"id": "req_9"},
            "account": "acct_test",
            "data": {"object": {"amount": 1000}},
        }

        class FakeReq:
            headers = {}

            async def body(self):
                return b""

        ev = _run(src.shape(FakeReq(), b"", payload))
        assert ev.id == "evt_1"
        assert ev.type == STRIPE_EVENT_RECEIVED_TYPE
        assert ev.subject == "charge.succeeded"
        assert ev.source == "stripe://acct_test"
        # The top-level Stripe fields survive: the envelope used to carry only
        # the inner ``data``, so livemode / created / api_version read None.
        assert ev.data == {
            "event_id": "evt_1",
            "event_type": "charge.succeeded",
            "created": 1700000000,
            "livemode": False,
            "api_version": "2024-04-10",
            "request_id": "req_9",
            "account": "acct_test",
            "data": {"object": {"amount": 1000}},
        }

    def test_account_is_set_only_for_connect_events(self):
        from nodes.stripe._source import shape_stripe_event

        assert shape_stripe_event({"id": "evt_1", "type": "charge.succeeded"})["account"] is None

    def test_handle_dispatches_and_emits_on_valid_signature(self):
        from nodes.stripe._events import STRIPE_EVENT_RECEIVED_TYPE
        from nodes.stripe._source import get_webhook_source

        src = get_webhook_source()
        body = json.dumps({"id": "evt_2", "type": "charge.succeeded", "data": {}}).encode()
        req = _fake_request(body, headers=_signed(body, self.SECRET))
        with (
            self._stub_secret_resolution(),
            patch("services.event_waiter.dispatch") as dispatch,
            patch("services.events.dispatch.emit", new=AsyncMock()) as emit,
        ):
            ev = _run(src.handle(req))
        assert ev.type == STRIPE_EVENT_RECEIVED_TYPE
        # Dispatched as a lone envelope: the two-argument form means
        # ``(event_type, data)``, so passing the event second made ``data``
        # the envelope and the dispatcher's ``data.get(...)`` calls raised
        # AttributeError once a waiter was active.
        (dispatched,) = dispatch.call_args[0]
        assert dispatched is ev
        # The waiter serves a canvas Run only. Deployed listeners are reached
        # by emit alone, which the base WebhookSource.handle never calls.
        emit.assert_awaited_once()
        assert emit.await_args.args == (ev,)
        assert emit.await_args.kwargs == {"wire_routing_key": "stripe_event_received"}

    def test_handle_rejects_tampered_signature(self):
        from fastapi import HTTPException
        from nodes.stripe._source import get_webhook_source

        src = get_webhook_source()
        body = json.dumps({"id": "evt_3", "type": "charge.succeeded"}).encode()
        req = _fake_request(body, headers=_signed(body, "whsec_other"))
        with self._stub_secret_resolution(), patch("services.events.dispatch.emit", new=AsyncMock()) as emit:
            with pytest.raises(HTTPException) as exc:
                _run(src.handle(req))
        assert exc.value.status_code == 400
        emit.assert_not_awaited()


# ============================================================================
# StripeReceiveNode — filter + reshape
# ============================================================================


def _stripe_payload(stripe_type: str = "charge.succeeded", *, event_id: str = "evt_1", livemode: bool = False) -> dict:
    """A Stripe event as ``stripe listen`` forwards it."""
    return {
        "id": event_id,
        "object": "event",
        "type": stripe_type,
        "created": 1700000000,
        "livemode": livemode,
        "api_version": "2024-04-10",
        "request": {"id": "req_1"},
        "data": {"object": {"id": "ch_1", "amount": 1000}},
    }


class TestStripeReceiveFilter:
    """Both callers hand the filter the envelope's ``data``: the event waiter
    on a canvas Run, and ``evaluate_trigger_filter_activity`` once deployed.
    The base filter rebuilt an envelope from that dict and raised."""

    def _filter(self, params: dict | None = None):
        from nodes.stripe.stripe_receive import StripeReceiveNode, StripeReceiveParams

        node = StripeReceiveNode()
        return node.build_filter(StripeReceiveParams(**(params or {})))

    def _data(self, stripe_type: str, livemode: bool = False) -> dict:
        from nodes.stripe._source import shape_stripe_event

        return shape_stripe_event(_stripe_payload(stripe_type, livemode=livemode))

    def test_all_matches_anything(self):
        f = self._filter({"event_type_filter": "all"})
        assert f(self._data("charge.succeeded")) is True
        assert f(self._data("payment_intent.created")) is True

    def test_exact_match(self):
        f = self._filter({"event_type_filter": "charge.succeeded"})
        assert f(self._data("charge.succeeded")) is True
        assert f(self._data("charge.refunded")) is False

    def test_wildcard_prefix(self):
        f = self._filter({"event_type_filter": "charge.*"})
        assert f(self._data("charge.succeeded")) is True
        assert f(self._data("charge.refunded")) is True
        assert f(self._data("payment_intent.created")) is False

    def test_stripe_prefixed_pattern_still_matches(self):
        """The node once required patterns to work with or without the
        ``stripe.`` prefix; saved workflows may carry either."""
        f = self._filter({"event_type_filter": "stripe.charge.*"})
        assert f(self._data("charge.refunded")) is True
        assert f(self._data("payment_intent.created")) is False

    def test_livemode_filter(self):
        live = self._filter({"livemode_filter": "live"})
        test = self._filter({"livemode_filter": "test"})
        assert live(self._data("charge.succeeded", livemode=True)) is True
        assert live(self._data("charge.succeeded", livemode=False)) is False
        assert test(self._data("charge.succeeded", livemode=False)) is True
        assert test(self._data("charge.succeeded", livemode=True)) is False

    def test_an_envelope_is_accepted_too(self):
        from nodes.stripe._events import stripe_event_received
        from datetime import datetime, timezone

        data = self._data("charge.succeeded")
        envelope = stripe_event_received(data, event_id="evt_1", account=None, time=datetime.now(timezone.utc))
        assert self._filter({"event_type_filter": "charge.*"})(envelope) is True


class TestStripeReceiveReshape:
    def test_shape_output_is_the_shaped_payload(self):
        """The source shapes; the node only unwraps, because a deployed
        trigger hands downstream nodes ``event.data`` verbatim."""
        from datetime import datetime, timezone

        from nodes.stripe._events import stripe_event_received
        from nodes.stripe._source import shape_stripe_event
        from nodes.stripe.stripe_receive import StripeReceiveNode

        data = shape_stripe_event(_stripe_payload())
        ev = stripe_event_received(data, event_id="evt_1", account=None, time=datetime.now(timezone.utc))
        out = StripeReceiveNode().shape_output(ev)
        assert out == data
        assert out["event_type"] == "charge.succeeded"
        assert out["request_id"] == "req_1"
        assert out["livemode"] is False
        assert out["data"] == {"object": {"id": "ch_1", "amount": 1000}}


# ============================================================================
# StripeReceiveNode — a canvas Run resolves on a real delivery
# ============================================================================


class TestCanvasRunResolves:
    """End to end through the real event waiter: ``execute`` registers a
    waiter, ``handle`` dispatches a signed delivery. Before the fix the waiter
    was keyed ``stripe.webhook``, which no envelope carries; the filter raised
    on the payload; and ``execute`` raised rebuilding an envelope from it."""

    SECRET = "whsec_run"

    def _scenario(self, params: dict, stripe_type: str):
        from nodes.stripe._source import get_webhook_source
        from nodes.stripe.stripe_receive import StripeReceiveNode
        from services import event_waiter

        node = StripeReceiveNode()
        body = json.dumps(_stripe_payload(stripe_type)).encode()

        async def run():
            task = asyncio.create_task(node.execute("stripe-run-node", params, None))
            # Let execute register its waiter before the delivery arrives.
            for _ in range(20):
                if any(w.node_id == "stripe-run-node" for w in event_waiter._waiters.values()):
                    break
                await asyncio.sleep(0)
            await get_webhook_source().handle(_fake_request(body, headers=_signed(body, self.SECRET)))
            try:
                return await asyncio.wait_for(asyncio.shield(task), timeout=0.5)
            except asyncio.TimeoutError:
                return None
            finally:
                event_waiter.cancel_for_node("stripe-run-node")
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        with (
            patch.object(StripeReceiveNode, "_check_precondition", AsyncMock(return_value=None)),
            patch(
                "nodes.stripe._source.StripeCredential.resolve",
                AsyncMock(return_value={"stripe_webhook_secret": self.SECRET}),
            ),
            patch("services.events.dispatch.emit", new=AsyncMock()),
        ):
            return _run(run())

    def test_matching_delivery_resolves_the_run(self):
        result = self._scenario({"event_type_filter": "charge.*"}, "charge.succeeded")
        assert result is not None, "the canvas Run never resolved"
        assert result["success"] is True, result
        assert result["result"]["event_id"] == "evt_1"
        assert result["result"]["event_type"] == "charge.succeeded"
        assert result["result"]["livemode"] is False
        assert result["result"]["data"] == {"object": {"id": "ch_1", "amount": 1000}}

    def test_filtered_delivery_leaves_the_run_waiting(self):
        assert self._scenario({"event_type_filter": "payment_intent.*"}, "charge.succeeded") is None


# ============================================================================
# StripeReceiveNode — the deployed path
# ============================================================================


class TestDeployedPath:
    def test_trigger_is_deployable(self):
        """find_trigger_nodes filters on this set. Omission means deploy
        silently ignores the node -- no listener, no error."""
        from constants import EVENT_TRIGGER_TYPES, WORKFLOW_TRIGGER_TYPES

        assert "stripeReceive" in WORKFLOW_TRIGGER_TYPES
        assert "stripeReceive" in EVENT_TRIGGER_TYPES

    def test_canary_type_matches_every_emitted_envelope(self):
        """The Visibility query asks for the envelope's type and the listener
        advertises the registered one; a mismatch delivers nothing, silently.
        Every Stripe event must therefore carry the same type."""
        from nodes.stripe._events import STRIPE_EVENT_RECEIVED_TYPE
        from nodes.stripe._source import get_webhook_source
        from nodes.stripe.stripe_receive import StripeReceiveNode
        from services.deployment.canary_registry import cloudevent_type_for

        assert cloudevent_type_for("stripeReceive") == STRIPE_EVENT_RECEIVED_TYPE
        assert StripeReceiveNode.event_type == STRIPE_EVENT_RECEIVED_TYPE
        for stripe_type in ("charge.succeeded", "payment_intent.created", "customer.subscription.deleted"):
            ev = _run(get_webhook_source().shape(None, b"", _stripe_payload(stripe_type)))
            assert ev.type == STRIPE_EVENT_RECEIVED_TYPE
            assert ev.subject == stripe_type

    def test_envelope_carries_no_workflow_id(self):
        """Setting it would scope delivery to one deployment; a Stripe event
        is meant to reach every deployment carrying the trigger."""
        from nodes.stripe._source import get_webhook_source

        ev = _run(get_webhook_source().shape(None, b"", _stripe_payload()))
        assert ev.workflow_id is None

    @pytest.mark.parametrize(
        ("filter_params", "stripe_type", "livemode", "admitted"),
        [
            ({"event_type_filter": "charge.*"}, "charge.refunded", False, True),
            ({"event_type_filter": "charge.*"}, "payment_intent.created", False, False),
            ({"livemode_filter": "live"}, "charge.succeeded", True, True),
            ({"livemode_filter": "live"}, "charge.succeeded", False, False),
        ],
    )
    def test_deployed_filter_applies_and_does_not_fail_open(self, filter_params, stripe_type, livemode, admitted):
        """The deployed listener runs the node's filter through this activity,
        which admits the event when the filter raises. The old filter raised on
        every Stripe payload, so deployed filters were silently ignored."""
        from nodes.stripe._source import shape_stripe_event
        from services.temporal.activities import evaluate_trigger_filter_activity

        data = shape_stripe_event(_stripe_payload(stripe_type, livemode=livemode))
        result = _run(
            evaluate_trigger_filter_activity(
                {"node_type": "stripeReceive", "filter_params": filter_params, "event_data": data}
            )
        )
        assert result is admitted


class TestPrepareDeployment:
    """Nothing else starts ``stripe listen`` after a backend restart, so the
    deploy (and boot re-arm) hook must, without holding up Start."""

    def _drain(self):
        from nodes.stripe import stripe_receive

        async def run(starter):
            with patch.object(stripe_receive, "_start_listen_daemon", starter):
                await stripe_receive.StripeReceiveNode.prepare_deployment(
                    node_id="n1", workflow_id="wf1", parameters={}
                )
                pending = list(stripe_receive._background_tasks)
                assert pending, "prepare_deployment scheduled nothing"
                await asyncio.gather(*pending)

        return run

    def test_starts_the_listen_daemon_in_the_background(self):
        starter = AsyncMock(return_value=None)
        _run(self._drain()(starter))
        starter.assert_awaited_once()

    def test_a_failed_start_is_logged_not_raised(self):
        _run(self._drain()(AsyncMock(return_value="Stripe not connected. Log in with Stripe in Credentials.")))
        _run(self._drain()(AsyncMock(side_effect=RuntimeError("download failed"))))


# ============================================================================
# StripeActionNode — pass-through over the CLI
# ============================================================================


class TestStripeActionPassthrough:
    @pytest.fixture
    def cli_capture(self):
        captured: list[list[str]] = []

        async def fake_run(*, binary, argv, **kwargs):
            # Stripe CLI uses ~/.config/stripe/config.toml — no credential injection.
            captured.append(list(argv))
            return {"success": True, "result": {"id": "x"}, "stdout": "{}"}

        return captured, fake_run

    def test_command_is_shlex_split(self, cli_capture):
        captured, fake = cli_capture
        with patch("nodes.stripe.stripe_action.run_cli_command", AsyncMock(side_effect=fake)):
            from nodes.stripe.stripe_action import StripeActionNode, StripeActionParams

            node = StripeActionNode()
            result = _run(node.run(None, StripeActionParams(command="customers create --email a@b.com")))
            assert result["success"] is True
            assert captured == [["customers", "create", "--email", "a@b.com"]]

    def test_quoted_args_preserved(self, cli_capture):
        captured, fake = cli_capture
        with patch("nodes.stripe.stripe_action.run_cli_command", AsyncMock(side_effect=fake)):
            from nodes.stripe.stripe_action import StripeActionNode, StripeActionParams

            node = StripeActionNode()
            _run(node.run(None, StripeActionParams(command="customers create --name 'Acme Inc'")))
            assert captured == [["customers", "create", "--name", "Acme Inc"]]

    def test_empty_command_raises(self):
        from nodes.stripe.stripe_action import StripeActionNode, StripeActionParams

        node = StripeActionNode()
        with pytest.raises(RuntimeError, match="command is required"):
            _run(node.run(None, StripeActionParams(command="   ")))

    def test_cli_failure_raises(self):
        from nodes.stripe.stripe_action import StripeActionNode, StripeActionParams

        async def fake_fail(*, binary, argv, **kwargs):
            return {"success": False, "error": "stripe: unknown command 'frobnicate'"}

        with patch("nodes.stripe.stripe_action.run_cli_command", AsyncMock(side_effect=fake_fail)):
            node = StripeActionNode()
            with pytest.raises(RuntimeError, match="frobnicate"):
                _run(node.run(None, StripeActionParams(command="frobnicate")))


# ============================================================================
# Plugin self-registration
# ============================================================================


class TestStripePluginRegistration:
    def test_ws_handlers_registered(self):
        import nodes.stripe  # noqa: F401
        from services.ws_handler_registry import get_ws_handlers

        registered = get_ws_handlers()
        for name in (
            "stripe_login",
            "stripe_logout",
            "stripe_connect",
            "stripe_disconnect",
            "stripe_reconnect",
            "stripe_status",
            "stripe_trigger",
        ):
            assert name in registered, f"WS handler '{name}' not registered"

    def test_webhook_source_registered(self):
        import nodes.stripe  # noqa: F401
        from services.events import WEBHOOK_SOURCES

        assert "stripe" in WEBHOOK_SOURCES
        assert WEBHOOK_SOURCES["stripe"].type == "stripe.webhook"

    def test_node_classes_registered(self):
        import nodes.stripe  # noqa: F401
        from services.node_registry import get_node_class

        assert get_node_class("stripeReceive").__name__ == "StripeReceiveNode"
        assert get_node_class("stripeAction").__name__ == "StripeActionNode"

    def test_credential_registered(self):
        import nodes.stripe  # noqa: F401
        from services.plugin.credential import CREDENTIAL_REGISTRY

        # Stripe CLI manages auth at ~/.config/stripe/config.toml; the
        # credential class is a thin marker keyed by "stripe" (no api_key).
        assert "stripe" in CREDENTIAL_REGISTRY

    def test_output_schemas_registered(self):
        import nodes.stripe  # noqa: F401
        from services.node_output_schemas import NODE_OUTPUT_SCHEMAS

        assert "stripeReceive" in NODE_OUTPUT_SCHEMAS
        assert "stripeAction" in NODE_OUTPUT_SCHEMAS

    def test_action_node_is_ai_tool(self):
        from nodes.stripe.stripe_action import StripeActionNode

        assert StripeActionNode.usable_as_tool is True

    def test_receive_node_waits_for_the_type_the_source_dispatches(self):
        """Not the source's own type (``stripe.webhook``): no envelope carries
        that, so a waiter keyed on it never resolved."""
        from nodes.stripe._events import STRIPE_EVENT_RECEIVED_TYPE
        from nodes.stripe.stripe_receive import StripeReceiveNode

        assert StripeReceiveNode.event_type == STRIPE_EVENT_RECEIVED_TYPE
        assert StripeReceiveNode.webhook_source.type == "stripe.webhook"

    def test_listen_source_has_correct_namespace(self):
        from nodes.stripe._source import get_listen_source

        src = get_listen_source()
        assert src.process_name == "stripe-listen"
        assert src.workflow_namespace == "_stripe"
        # Empty binary_name disables the framework's PATH check; the
        # plugin resolves the binary itself via ensure_stripe_cli (which
        # falls back to an OS-cache download via
        # ``core.paths.package_dir('stripe')`` on systems without a
        # system install of the Stripe CLI).
        assert src.binary_name == ""
