"""Contract tests for the Dodo Payments action node (``dodoPaymentsAction``).

The node calls Dodo through the official ``dodopayments`` SDK; every SDK touch
lives in ``nodes/dodo_payments/_sdk.py``. These tests run the real SDK and
intercept its HTTP traffic with respx, so requests are built exactly as the
SDK builds them and nothing touches the network. Base URLs come from the
SDK's own ``ENVIRONMENTS``.
"""

from __future__ import annotations

import inspect
import json
import re
import subprocess
import sys
import typing
from pathlib import Path
from typing import get_args
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx
from dodopayments import ENVIRONMENTS

from tests.nodes._mocks import patched_container

pytestmark = pytest.mark.node_contract

import nodes.dodo_payments as dodo_pkg  # noqa: E402
from nodes.dodo_payments import _sdk, dodo_payments_action  # noqa: E402
from nodes.dodo_payments._credentials import MODE_FIELD, MODES, DodoPaymentsCredential  # noqa: E402
from services.node_registry import get_node_class  # noqa: E402
from services.plugin.credential import CREDENTIAL_REGISTRY  # noqa: E402
from services.ws_handler_registry import get_option_loader  # noqa: E402

PLUGIN_DIR = Path(dodo_pkg.__file__).parent
SERVER_DIR = PLUGIN_DIR.parents[1]

TEST_URL = ENVIRONMENTS["test_mode"]
LIVE_URL = ENVIRONMENTS["live_mode"]
KEYS = {"dodo_payments": "dodo_test_key", MODE_FIELD: "test_mode"}


@pytest.fixture(autouse=True)
def no_read_retries(monkeypatch):
    """Retries back off for half a second each; tests that need them opt in."""
    monkeypatch.setattr(_sdk, "READ_RETRIES", 0)


async def _execute(harness, params, *, keys=KEYS, **kwargs):
    with patched_container(auth_api_keys=keys):
        return await harness.execute("dodoPaymentsAction", params, **kwargs)


def _body(request: httpx.Request) -> dict:
    return json.loads(request.content or b"{}")


# ============================================================================
# Registration
# ============================================================================


class TestRegistration:
    def test_class_attributes(self):
        cls = get_node_class("dodoPaymentsAction")
        assert cls is not None, "dodoPaymentsAction is not registered"
        assert cls.tool_name == "dodo_payments_action"
        assert tuple(cls.group) == ("payments", "tool")
        assert cls.usable_as_tool is True
        assert DodoPaymentsCredential in tuple(cls.credentials)
        assert cls.annotations["destructive"] is True

    def test_both_canvas_handles_stay_visible(self):
        cls = get_node_class("dodoPaymentsAction")
        assert cls.hide_input_handle is False
        assert cls.hide_output_handle is False
        assert {h["name"] for h in cls.handles} == {"input-main", "output-main"}

    def test_option_loader_and_credential_registered(self):
        assert get_option_loader("dodoPaymentsEndpoints") is not None
        assert CREDENTIAL_REGISTRY["dodo_payments"] is DodoPaymentsCredential

    def test_tool_schema_is_flat(self):
        from services.plugin.tool import inline_schema_refs

        cls = get_node_class("dodoPaymentsAction")
        dumped = json.dumps(inline_schema_refs(cls.Params.model_json_schema()))
        assert "$defs" not in dumped
        assert '"$ref"' not in dumped

    def test_operations_match_the_literal_and_default_to_a_read(self):
        cls = get_node_class("dodoPaymentsAction")
        declared = set(get_args(cls.Params.model_fields["operation"].annotation))
        assert declared == set(dodo_payments_action._OPERATIONS)
        # The approval rule reads the literal operation, so a default that
        # moved money would slip through when a model omits it.
        assert cls.Params().operation == "list_payments"
        assert cls.Params().operation not in dodo_payments_action._HELD_OPERATIONS

    def test_reserved_field_names_absent(self):
        # ParameterRenderer keys magic on these literal names.
        cls = get_node_class("dodoPaymentsAction")
        assert not {"model", "api_key", "parameters", "action", "service_id", "session_id"} & set(cls.Params.model_fields)

    @pytest.mark.parametrize(
        ("module", "typed_dict", "key", "ours"),
        [
            ("payment_list_params", "PaymentListParams", "status", dodo_payments_action.PAYMENT_STATUSES),
            ("subscription_list_params", "SubscriptionListParams", "status", dodo_payments_action.SUBSCRIPTION_STATUSES),
            ("refund_list_params", "RefundListParams", "status", dodo_payments_action.REFUND_STATUSES),
            (
                "subscription_change_plan_params",
                "SubscriptionChangePlanParams",
                "proration_billing_mode",
                dodo_payments_action.PRORATION_BILLING_MODES,
            ),
        ],
    )
    def test_dropdown_values_follow_the_sdk(self, module, typed_dict, key, ours):
        import importlib

        params = getattr(importlib.import_module(f"dodopayments.types.{module}"), typed_dict)
        assert set(get_args(typing.get_type_hints(params)[key])) == set(ours)

    def test_modes_are_the_sdk_environments(self):
        assert set(MODES) == set(ENVIRONMENTS)
        assert MODES[0] == "test_mode", "validation must try the test mode first"


# ============================================================================
# Credential
# ============================================================================


class TestCredentialProbe:
    @respx.mock
    async def test_a_test_key_is_found_in_test_mode(self):
        test = respx.get(f"{TEST_URL}/brands").mock(return_value=httpx.Response(200, json={"items": []}))

        result = await DodoPaymentsCredential._probe("dodo_test_key")

        assert result.valid is True
        assert result.extra == {"mode": "test_mode"}
        assert "test mode" in result.message
        assert test.calls.last.request.headers["Authorization"] == "Bearer dodo_test_key"

    @respx.mock
    async def test_a_live_key_is_found_in_live_mode(self):
        respx.get(f"{TEST_URL}/brands").mock(return_value=httpx.Response(401, json={"code": "UNAUTHORIZED"}))
        respx.get(f"{LIVE_URL}/brands").mock(return_value=httpx.Response(200, json={"items": []}))

        result = await DodoPaymentsCredential._probe("dodo_live_key")

        assert result.valid is True
        assert result.extra == {"mode": "live_mode"}

    @respx.mock
    async def test_a_key_both_modes_refuse_is_invalid(self):
        respx.get(f"{TEST_URL}/brands").mock(return_value=httpx.Response(401))
        respx.get(f"{LIVE_URL}/brands").mock(return_value=httpx.Response(401))

        result = await DodoPaymentsCredential._probe("wrong")

        assert result.valid is False
        assert "both test and live mode" in result.message

    @respx.mock
    async def test_another_refusal_carries_dodos_code(self):
        respx.get(f"{TEST_URL}/brands").mock(return_value=httpx.Response(401))
        respx.get(f"{LIVE_URL}/brands").mock(
            return_value=httpx.Response(403, json={"code": "MERCHANT_NOT_LIVE", "message": "Business is not live"})
        )

        result = await DodoPaymentsCredential._probe("dodo_live_key")

        assert result.valid is False
        assert "MERCHANT_NOT_LIVE" in result.message


class TestCredentialValidate:
    def _broadcaster(self):
        broadcaster = MagicMock(name="StatusBroadcaster")
        broadcaster.update_api_key_status = AsyncMock()
        return patch("services.status_broadcaster.get_status_broadcaster", return_value=broadcaster)

    @respx.mock
    async def test_saves_the_mode_beside_the_key(self):
        respx.get(f"{TEST_URL}/brands").mock(return_value=httpx.Response(401))
        respx.get(f"{LIVE_URL}/brands").mock(return_value=httpx.Response(200, json={"items": []}))

        with patched_container() as container, self._broadcaster():
            response = await DodoPaymentsCredential.validate({"api_key": "dodo_live_key"})
            stored = container.auth_service().store_api_key.await_args_list

        assert response["valid"] is True
        assert response["mode"] == "live_mode"
        assert [call.kwargs.get("provider", call.args[0] if call.args else None) for call in stored] == [
            "dodo_payments",
            MODE_FIELD,
        ]
        assert stored[1].args[1] == "live_mode"

    @respx.mock
    async def test_a_rejected_key_saves_nothing(self):
        respx.get(f"{TEST_URL}/brands").mock(return_value=httpx.Response(401))
        respx.get(f"{LIVE_URL}/brands").mock(return_value=httpx.Response(401))

        with patched_container() as container, self._broadcaster():
            response = await DodoPaymentsCredential.validate({"api_key": "wrong"})
            store = container.auth_service().store_api_key

        assert response["valid"] is False
        store.assert_not_awaited()


# ============================================================================
# Reads
# ============================================================================


class TestReads:
    async def test_missing_key_points_at_the_modal(self, harness):
        result = await _execute(harness, {"operation": "list_payments"}, keys={})

        harness.assert_envelope(result, success=False)
        assert result["error_type"] == "PermissionDeniedError"
        assert result["credential"]["provider"] == "dodo_payments"

    async def test_a_key_without_its_mode_asks_for_validation(self, harness):
        result = await _execute(harness, {"operation": "list_payments"}, keys={"dodo_payments": "dodo_test_key"})

        harness.assert_envelope(result, success=False)
        assert result["error_type"] == "PermissionDeniedError"
        assert "Validate" in result["error"]

    @respx.mock
    async def test_list_payments_sends_one_page_with_the_filters(self, harness):
        route = respx.get(f"{TEST_URL}/payments").mock(
            return_value=httpx.Response(200, json={"items": [{"payment_id": "pay_1", "status": "failed"}]})
        )

        result = await _execute(
            harness,
            {
                "operation": "list_payments",
                "customer_id": "cus_1",
                "payment_status": "failed",
                "page": 1,
                "page_size": 5,
            },
        )

        harness.assert_envelope(result, success=True)
        request = route.calls.last.request
        assert dict(request.url.params) == {
            "page_number": "1",
            "page_size": "5",
            "customer_id": "cus_1",
            "status": "failed",
        }
        assert request.headers["Authorization"] == "Bearer dodo_test_key"
        assert result["result"] == {
            "operation": "list_payments",
            "mode": "test_mode",
            "result": [{"payment_id": "pay_1", "status": "failed"}],
            "count": 1,
            "page": 1,
            "page_size": 5,
        }

    @respx.mock
    async def test_a_live_key_calls_the_live_api(self, harness):
        route = respx.get(f"{LIVE_URL}/payments").mock(return_value=httpx.Response(200, json={"items": []}))

        result = await _execute(harness, {"operation": "list_payments"}, keys={"dodo_payments": "k", MODE_FIELD: "live_mode"})

        harness.assert_envelope(result, success=True)
        assert route.call_count == 1
        assert result["result"]["mode"] == "live_mode"
        assert result["result"]["result"] == []

    @respx.mock
    async def test_get_product_reads_it_by_id(self, harness):
        route = respx.get(f"{TEST_URL}/products/pdt_1").mock(
            return_value=httpx.Response(200, json={"product_id": "pdt_1", "name": "Pro"})
        )

        result = await _execute(harness, {"operation": "get_product", "product_id": "pdt_1"})

        harness.assert_envelope(result, success=True)
        assert route.call_count == 1
        assert result["result"]["result"]["name"] == "Pro"

    @respx.mock
    async def test_a_missing_id_fails_before_any_request(self, harness):
        result = await _execute(harness, {"operation": "get_payment"})

        harness.assert_envelope(result, success=False)
        assert result["error_type"] == "NodeUserError"
        assert "payment_id is required" in result["error"]
        assert not respx.calls

    @respx.mock
    async def test_reads_are_retried(self, harness, monkeypatch):
        monkeypatch.setattr(_sdk, "READ_RETRIES", 1)
        route = respx.get(f"{TEST_URL}/payments").mock(
            side_effect=[httpx.Response(500), httpx.Response(200, json={"items": []})]
        )

        result = await _execute(harness, {"operation": "list_payments"})

        harness.assert_envelope(result, success=True)
        assert route.call_count == 2


# ============================================================================
# Writes
# ============================================================================


class TestWrites:
    @respx.mock
    async def test_checkout_session_for_a_new_customer(self, harness):
        route = respx.post(f"{TEST_URL}/checkouts").mock(
            return_value=httpx.Response(200, json={"session_id": "cks_1", "checkout_url": "https://checkout.example/1"})
        )

        result = await _execute(
            harness,
            {
                "operation": "create_checkout_session",
                "product_id": "pdt_1",
                "quantity": 2,
                "customer_email": "ada@example.com",
                "customer_name": "Ada",
                "return_url": "https://example.com/thanks",
                "discount_code": "LAUNCH",
            },
        )

        harness.assert_envelope(result, success=True)
        assert _body(route.calls.last.request) == {
            "product_cart": [{"product_id": "pdt_1", "quantity": 2}],
            "customer": {"email": "ada@example.com", "name": "Ada"},
            "return_url": "https://example.com/thanks",
            "discount_codes": ["LAUNCH"],
        }
        assert result["result"]["result"]["checkout_url"] == "https://checkout.example/1"

    @respx.mock
    async def test_checkout_session_for_an_existing_customer(self, harness):
        route = respx.post(f"{TEST_URL}/checkouts").mock(return_value=httpx.Response(200, json={"session_id": "cks_1"}))

        await _execute(
            harness,
            {"operation": "create_checkout_session", "product_id": "pdt_1", "customer_id": "cus_1", "customer_email": "x@y.z"},
        )

        assert _body(route.calls.last.request) == {
            "product_cart": [{"product_id": "pdt_1", "quantity": 1}],
            "customer": {"customer_id": "cus_1"},
        }

    @respx.mock
    async def test_create_customer_needs_email_and_name(self, harness):
        result = await _execute(harness, {"operation": "create_customer", "customer_email": "ada@example.com"})

        harness.assert_envelope(result, success=False)
        assert "customer_name is required" in result["error"]
        assert not respx.calls

    @respx.mock
    async def test_portal_link_never_emails_the_customer(self, harness):
        route = respx.post(f"{TEST_URL}/customers/cus_1/customer-portal/session").mock(
            return_value=httpx.Response(200, json={"link": "https://portal.example/1"})
        )

        result = await _execute(harness, {"operation": "create_customer_portal_link", "customer_id": "cus_1"})

        harness.assert_envelope(result, success=True)
        assert route.calls.last.request.url.params["send_email"] == "false"
        assert result["result"]["result"] == {"link": "https://portal.example/1"}

    @respx.mock
    async def test_refund_of_the_whole_payment(self, harness):
        route = respx.post(f"{TEST_URL}/refunds").mock(
            return_value=httpx.Response(200, json={"refund_id": "ref_1", "status": "pending"})
        )

        result = await _execute(harness, {"operation": "create_refund", "payment_id": "pay_1", "reason": "Duplicate"})

        harness.assert_envelope(result, success=True)
        assert _body(route.calls.last.request) == {"payment_id": "pay_1", "reason": "Duplicate"}

    @respx.mock
    async def test_refund_of_part_of_an_item(self, harness):
        route = respx.post(f"{TEST_URL}/refunds").mock(return_value=httpx.Response(200, json={"refund_id": "ref_1"}))

        await _execute(harness, {"operation": "create_refund", "payment_id": "pay_1", "product_id": "pdt_1", "amount": 500})

        assert _body(route.calls.last.request) == {
            "payment_id": "pay_1",
            "items": [{"item_id": "pdt_1", "amount": 500}],
        }

    @respx.mock
    async def test_a_partial_refund_without_its_item_is_refused(self, harness):
        result = await _execute(harness, {"operation": "create_refund", "payment_id": "pay_1", "amount": 500})

        harness.assert_envelope(result, success=False)
        assert "product_id" in result["error"]
        assert not respx.calls

    @pytest.mark.parametrize(
        ("at_period_end", "body"),
        [(False, {"status": "cancelled"}), (True, {"cancel_at_next_billing_date": True})],
    )
    @respx.mock
    async def test_cancel_subscription(self, harness, at_period_end, body):
        route = respx.patch(f"{TEST_URL}/subscriptions/sub_1").mock(
            return_value=httpx.Response(200, json={"subscription_id": "sub_1"})
        )

        await _execute(
            harness,
            {"operation": "cancel_subscription", "subscription_id": "sub_1", "cancel_at_next_billing_date": at_period_end},
        )

        assert _body(route.calls.last.request) == body

    @respx.mock
    async def test_change_subscription_plan(self, harness):
        route = respx.post(f"{TEST_URL}/subscriptions/sub_1/change-plan").mock(return_value=httpx.Response(200, json={}))

        await _execute(
            harness,
            {
                "operation": "change_subscription_plan",
                "subscription_id": "sub_1",
                "product_id": "pdt_2",
                "quantity": 3,
                "proration_billing_mode": "do_not_bill",
            },
        )

        assert _body(route.calls.last.request) == {
            "product_id": "pdt_2",
            "quantity": 3,
            "proration_billing_mode": "do_not_bill",
        }

    @respx.mock
    async def test_writes_are_never_retried(self, harness, monkeypatch):
        """The SDK retries POSTs too and Dodo takes no idempotency key: a
        retried refund could be refunded twice."""
        monkeypatch.setattr(_sdk, "READ_RETRIES", 2)
        route = respx.post(f"{TEST_URL}/refunds").mock(return_value=httpx.Response(500, json={"code": "INTERNAL"}))

        result = await _execute(harness, {"operation": "create_refund", "payment_id": "pay_1"})

        harness.assert_envelope(result, success=False)
        assert route.call_count == 1
        assert "may or may not have been made" in result["error"]


# ============================================================================
# Errors
# ============================================================================


class TestErrors:
    @respx.mock
    async def test_a_rejected_key_points_at_the_modal(self, harness):
        respx.get(f"{TEST_URL}/payments").mock(return_value=httpx.Response(401, json={"code": "UNAUTHORIZED"}))

        result = await _execute(harness, {"operation": "list_payments"})

        harness.assert_envelope(result, success=False)
        assert result["error_type"] == "NodeUserError"
        assert "Credentials -> Dodo Payments" in result["error"]

    @respx.mock
    async def test_dodos_code_and_message_come_through(self, harness):
        respx.get(f"{TEST_URL}/payments/pay_x").mock(
            return_value=httpx.Response(404, json={"code": "NOT_FOUND", "message": "Payment not found"})
        )

        result = await _execute(harness, {"operation": "get_payment", "payment_id": "pay_x"})

        harness.assert_envelope(result, success=False)
        assert "payments.retrieve failed (404 NOT_FOUND): Payment not found" in result["error"]

    @respx.mock
    async def test_a_read_only_key_on_a_write(self, harness):
        respx.post(f"{TEST_URL}/refunds").mock(
            return_value=httpx.Response(403, json={"code": "UNAUTHORIZED", "message": "Write access disabled"})
        )

        result = await _execute(harness, {"operation": "create_refund", "payment_id": "pay_1"})

        harness.assert_envelope(result, success=False)
        assert "read-only key" in result["error"]


# ============================================================================
# call + list_operations
# ============================================================================


class TestCall:
    async def test_an_unknown_endpoint_suggests_close_names_before_credentials(self, harness):
        result = await _execute(harness, {"operation": "call", "endpoint": "payout.list"}, keys={})

        harness.assert_envelope(result, success=False)
        assert result["error_type"] == "NodeUserError"
        assert "payouts.list" in result["error"]

    @pytest.mark.parametrize(
        ("endpoint", "request_args", "expected"),
        [
            ("payouts.list", {"bogus": 1}, "unknown: bogus"),
            ("refunds.retrieve", {}, "missing: refund_id"),
            ("payouts.list", {"extra_headers": {"Authorization": "Bearer other"}}, "unknown: extra_headers"),
        ],
    )
    @respx.mock
    async def test_the_request_is_checked_before_any_network_use(self, harness, endpoint, request_args, expected):
        result = await _execute(harness, {"operation": "call", "endpoint": endpoint, "request": request_args})

        harness.assert_envelope(result, success=False)
        assert expected in result["error"]
        assert not respx.calls

    @respx.mock
    async def test_a_get_endpoint_answers_with_the_api_json(self, harness):
        route = respx.get(f"{TEST_URL}/payouts").mock(return_value=httpx.Response(200, json={"items": [{"payout_id": "po_1"}]}))

        result = await _execute(
            harness, {"operation": "call", "endpoint": "payouts.list", "request": json.dumps({"page_size": 1})}
        )

        harness.assert_envelope(result, success=True)
        assert route.calls.last.request.url.params["page_size"] == "1"
        assert result["result"] == {
            "operation": "call",
            "mode": "test_mode",
            "endpoint": "payouts.list",
            "result": {"items": [{"payout_id": "po_1"}]},
        }

    @respx.mock
    async def test_a_writing_endpoint_runs_without_retries(self, harness, monkeypatch):
        monkeypatch.setattr(_sdk, "READ_RETRIES", 2)
        route = respx.post(f"{TEST_URL}/subscriptions/sub_1/charge").mock(return_value=httpx.Response(502))

        result = await _execute(
            harness,
            {"operation": "call", "endpoint": "subscriptions.charge", "request": {"subscription_id": "sub_1", "product_price": 900}},
        )

        harness.assert_envelope(result, success=False)
        assert route.call_count == 1

    async def test_webhook_endpoints_are_not_reachable(self, harness):
        result = await _execute(harness, {"operation": "call", "endpoint": "webhooks.retrieve_secret"})

        harness.assert_envelope(result, success=False)
        assert "Unknown Dodo Payments endpoint" in result["error"]

    @respx.mock
    async def test_a_file_answer_is_refused(self, harness):
        respx.get(f"{TEST_URL}/invoices/payments/pay_1").mock(
            return_value=httpx.Response(200, content=b"%PDF-1.7", headers={"content-type": "application/pdf"})
        )

        result = await _execute(
            harness, {"operation": "call", "endpoint": "invoices.payments.retrieve", "request": {"payment_id": "pay_1"}}
        )

        harness.assert_envelope(result, success=False)
        assert "invoice PDF" in result["error"]


class TestListOperations:
    async def test_needs_no_credentials(self, harness):
        result = await _execute(harness, {"operation": "list_operations", "search": "refund"}, keys={})

        harness.assert_envelope(result, success=True)
        payload = result["result"]
        endpoints = {entry["endpoint"]: entry for entry in payload["result"]}
        assert payload["count"] == len(endpoints)
        assert endpoints["refunds.create"]["http_method"] == "POST"
        assert endpoints["refunds.create"]["reads"] is False
        assert {"name": "payment_id", "type": "str", "required": True} in endpoints["refunds.create"]["params"]
        assert endpoints["refunds.list"]["reads"] is True

    def test_every_sdk_method_has_a_route(self):
        """A method the route pattern misses would silently vanish from the
        index, and so from `call` and the approval rule."""
        from dodopayments import DodoPayments
        from dodopayments._resource import SyncAPIResource

        missed = []

        def walk(path, resource):
            for name, fn in inspect.getmembers(type(resource), inspect.isfunction):
                if not name.startswith("_") and _sdk._describe(f"{path}.{name}", fn) is None:
                    missed.append(f"{path}.{name}")
            for name, child in _sdk._resources(resource, SyncAPIResource):
                walk(f"{path}.{name}", child)

        with DodoPayments(bearer_token="x", base_url="http://introspection.invalid") as client:
            for name, resource in _sdk._resources(client, SyncAPIResource):
                if name not in _sdk._EXCLUDED_RESOURCES:
                    walk(name, resource)
        assert missed == []

    def test_the_index_matches_the_typed_operations(self):
        index = {entry.endpoint: entry for entry in _sdk.endpoint_index()}
        assert not [name for name in index if name.startswith("webhooks.")]
        for endpoint, method in {
            "payments.list": "GET",
            "payments.retrieve": "GET",
            "products.retrieve": "GET",
            "checkout_sessions.create": "POST",
            "customers.customer_portal.create": "POST",
            "refunds.create": "POST",
            "subscriptions.update": "PATCH",
            "subscriptions.change_plan": "POST",
        }.items():
            assert index[endpoint].http_method == method, endpoint

    async def test_the_dropdown_lists_every_endpoint(self):
        options = await get_option_loader("dodoPaymentsEndpoints")({})
        assert len(options) == len(_sdk.endpoint_index())
        assert {"value": "payouts.list", "label": "payouts.list", "description": "GET /payouts"} in options


# ============================================================================
# Approval rule
# ============================================================================


class TestApproval:
    @pytest.fixture
    def spec(self):
        return get_node_class("dodoPaymentsAction").approval

    @pytest.mark.parametrize(
        "operation",
        ["list_payments", "get_payment", "create_checkout_session", "create_customer", "create_customer_portal_link", "list_operations"],
    )
    def test_reads_and_harmless_writes_run(self, spec, operation):
        assert spec.sends({"operation": operation}) is False

    @pytest.mark.parametrize("operation", ["create_refund", "cancel_subscription", "change_subscription_plan"])
    def test_money_moves_wait_for_the_owner(self, spec, operation):
        assert spec.sends({"operation": operation}) is True

    @pytest.mark.parametrize(
        ("endpoint", "held"),
        [("payouts.list", False), ("subscriptions.charge", True), ("discounts.delete", True), ("no.such", True)],
    )
    def test_call_waits_only_when_its_endpoint_writes(self, spec, endpoint, held):
        assert spec.sends({"operation": "call", "endpoint": endpoint}) is held

    def test_the_card_shows_flat_details(self, spec):
        preview = spec.preview({"operation": "create_refund", "payment_id": "pay_1", "amount": 500, "reason": "Duplicate"})
        assert preview["channel"] == "Dodo Payments"
        assert preview["recipient_label"] == "the customer"
        assert {"label": "Payment", "value": "pay_1"} in preview["details"]
        assert {"label": "Amount", "value": "500"} in preview["details"]


# ============================================================================
# Import laziness
# ============================================================================


class TestNoEagerSdkImport:
    """Importing the plugin must never import the SDK (clean interpreter: the
    pytest process already loaded it above)."""

    def _probe(self, code: str) -> str:
        result = subprocess.run([sys.executable, "-c", code], cwd=SERVER_DIR, capture_output=True, text=True, timeout=180)
        assert result.returncode == 0, f"probe subprocess failed (rc={result.returncode}):\n{result.stderr}"
        return result.stdout

    def test_plugin_registers_without_the_sdk(self):
        out = self._probe(
            "import sys\n"
            "sys.modules['dodopayments'] = None\n"
            "import nodes.dodo_payments\n"
            "from services.node_registry import get_node_class\n"
            "print('REGISTERED=' + str(get_node_class('dodoPaymentsAction') is not None))\n"
        )
        assert "REGISTERED=True" in out

    def test_plugin_import_does_not_load_the_sdk(self):
        out = self._probe("import sys\nimport nodes.dodo_payments\nprint('LEAKED=' + str('dodopayments' in sys.modules))\n")
        assert "LEAKED=False" in out


# ============================================================================
# Catalogue + assets
# ============================================================================


class TestCatalogueAndAssets:
    def test_credential_providers_entry(self):
        config = json.loads((SERVER_DIR / "config" / "credential_providers.json").read_text(encoding="utf-8"))
        entry = config["providers"]["dodo_payments"]
        assert entry["kind"] == "apiKey"
        assert entry["category"] == "payments"
        assert entry["icon_ref"] == "/api/schemas/credentials/dodo_payments/icon"
        assert [f["key"] for f in entry["fields"]] == ["apiKey", "dodo_payments_webhook_key"]
        assert all(f["secret"] is True for f in entry["fields"])
        # The mode is written by validation, never shown as a field.
        assert tuple(DodoPaymentsCredential.extra_fields) == (MODE_FIELD, "dodo_payments_webhook_key")

    def test_tool_name_snapshot(self):
        snapshot = json.loads((SERVER_DIR / "tests" / "fixtures" / "tool_names_snapshot.json").read_text(encoding="utf-8"))
        assert snapshot["dodoPaymentsAction"] == "dodo_payments_action"

    def test_plugin_folder_assets(self):
        meta = json.loads((PLUGIN_DIR / "meta.json").read_text(encoding="utf-8"))
        assert re.fullmatch(r"#[0-9a-fA-F]{6}", meta["color"])
        assert "<svg" in (PLUGIN_DIR / "icon.svg").read_text(encoding="utf-8")
        assert DodoPaymentsCredential.get_icon_path() == PLUGIN_DIR / "dodo_payments.svg"
