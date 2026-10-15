"""Dodo Payments Action — typed payment operations over the official
dodopayments SDK, plus a passthrough to any SDK endpoint.

Dual-purpose: a workflow node and the AI tool ``dodo_payments_action``. Auth
is the API key saved in Credentials -> Dodo Payments (``_credentials.py``),
passed explicitly to every SDK client against the base URL of the mode the
key was validated in. Every SDK touch lives in ``_sdk.py``, imported lazily.
Results are Dodo's own response fields.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Literal, Mapping, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.plugin import ActionNode, NodeContext, NodeUserError, Operation, TaskQueue, coerce_blank_params
from services.plugin.approval import ApprovalSpec

from . import _sdk
from ._credentials import DodoPaymentsCredential, stored_mode

_OPERATIONS = (
    "list_payments",
    "get_payment",
    "list_subscriptions",
    "get_subscription",
    "list_customers",
    "get_customer",
    "list_products",
    "get_product",
    "list_refunds",
    "list_disputes",
    "create_checkout_session",
    "create_customer",
    "create_customer_portal_link",
    "create_refund",
    "cancel_subscription",
    "change_subscription_plan",
    "call",
    "list_operations",
)

# Calls that move money or change a customer's billing wait for the owner
# while the workflow asks first; `call` does only when its endpoint writes.
_HELD_OPERATIONS = frozenset({"create_refund", "cancel_subscription", "change_subscription_plan", "call"})

# The SDK's own status and proration values (types/*_params.py); a test keeps
# these in step with the installed SDK.
PAYMENT_STATUSES = (
    "succeeded",
    "failed",
    "cancelled",
    "processing",
    "requires_customer_action",
    "requires_merchant_action",
    "requires_payment_method",
    "requires_confirmation",
    "requires_capture",
    "partially_captured",
    "partially_captured_and_capturable",
)
SUBSCRIPTION_STATUSES = ("pending", "active", "on_hold", "paused", "cancelled", "failed", "expired", "past_due")
REFUND_STATUSES = ("succeeded", "failed", "pending", "review")
PRORATION_BILLING_MODES = (
    "prorated_immediately",
    "full_immediately",
    "difference_immediately",
    "do_not_bill",
    "prorated_immediately_keep_cycle",
)


def _show(*ops: str) -> Dict[str, Any]:
    return {"displayOptions": {"show": {"operation": list(ops)}}}


_LISTS = ("list_payments", "list_subscriptions", "list_customers", "list_products", "list_refunds", "list_disputes")


def _given(fields: Mapping[str, Any]) -> Dict[str, Any]:
    """The fields that carry a value; blanks are left for Dodo's defaults."""
    return {key: value for key, value in fields.items() if value not in (None, "")}


class DodoPaymentsActionParams(BaseModel):
    operation: Literal[_OPERATIONS] = "list_payments"

    payment_id: str = Field(
        default="",
        description="Payment id",
        json_schema_extra={"placeholder": "pay_...", **_show("get_payment", "create_refund")},
    )
    subscription_id: str = Field(
        default="",
        description="Subscription id (a filter for list_payments and list_refunds)",
        json_schema_extra={
            "placeholder": "sub_...",
            **_show(
                "get_subscription", "cancel_subscription", "change_subscription_plan", "list_payments", "list_refunds"
            ),
        },
    )
    customer_id: str = Field(
        default="",
        description=(
            "Customer id. create_checkout_session: an existing customer (else customer_email). "
            "A filter for the payment, subscription, refund and dispute lists"
        ),
        json_schema_extra={
            "placeholder": "cus_...",
            **_show(
                "get_customer",
                "create_customer_portal_link",
                "create_checkout_session",
                "list_payments",
                "list_subscriptions",
                "list_refunds",
                "list_disputes",
            ),
        },
    )
    product_id: str = Field(
        default="",
        description=(
            "Product id. create_checkout_session: what the customer buys; change_subscription_plan: the new plan; "
            "create_refund: the item to refund (with amount for part of it). A filter for the payment and subscription lists"
        ),
        json_schema_extra={
            "placeholder": "pdt_...",
            **_show(
                "get_product",
                "create_checkout_session",
                "change_subscription_plan",
                "create_refund",
                "list_payments",
                "list_subscriptions",
            ),
        },
    )
    quantity: int = Field(
        default=1,
        ge=1,
        description="Units of the product",
        json_schema_extra=_show("create_checkout_session", "change_subscription_plan"),
    )
    customer_email: str = Field(
        default="",
        description="Customer email. create_checkout_session: a new customer; list_customers: a filter",
        json_schema_extra={
            "placeholder": "ada@example.com",
            **_show("create_checkout_session", "create_customer", "list_customers"),
        },
    )
    customer_name: str = Field(
        default="",
        description="Customer name (required by create_customer)",
        json_schema_extra={"placeholder": "Ada Lovelace", **_show("create_checkout_session", "create_customer")},
    )
    return_url: str = Field(
        default="",
        description="Where the customer lands afterwards",
        json_schema_extra={
            "placeholder": "https://example.com/thanks",
            **_show("create_checkout_session", "create_customer_portal_link"),
        },
    )
    discount_code: str = Field(
        default="",
        description="Discount code to apply",
        json_schema_extra={"placeholder": "LAUNCH20", **_show("create_checkout_session")},
    )
    amount: Optional[int] = Field(
        default=None,
        ge=1,
        description="Part of the product's price to refund, in the currency's smallest unit (cents); empty refunds it all",
        json_schema_extra=_show("create_refund"),
    )
    reason: str = Field(
        default="",
        description="Why the refund is made",
        json_schema_extra={"placeholder": "Duplicate order", **_show("create_refund")},
    )
    cancel_at_next_billing_date: bool = Field(
        default=False,
        description="Keep the subscription until the end of the paid period instead of cancelling now",
        json_schema_extra=_show("cancel_subscription"),
    )
    proration_billing_mode: Literal[PRORATION_BILLING_MODES] = Field(
        default="prorated_immediately",
        description="How the plan change is billed",
        json_schema_extra=_show("change_subscription_plan"),
    )
    payment_status: Literal[("all",) + PAYMENT_STATUSES] = Field(
        default="all",
        description="Only payments in this status",
        json_schema_extra=_show("list_payments"),
    )
    subscription_status: Literal[("all",) + SUBSCRIPTION_STATUSES] = Field(
        default="all",
        description="Only subscriptions in this status",
        json_schema_extra=_show("list_subscriptions"),
    )
    refund_status: Literal[("all",) + REFUND_STATUSES] = Field(
        default="all",
        description="Only refunds in this status",
        json_schema_extra=_show("list_refunds"),
    )
    page: int = Field(default=0, ge=0, description="Page number, from 0", json_schema_extra=_show(*_LISTS))
    page_size: int = Field(default=20, ge=1, le=100, description="Items per page", json_schema_extra=_show(*_LISTS))

    # call / list_operations
    endpoint: str = Field(
        default="",
        description="The SDK method as a dotted name, e.g. payouts.list or discounts.create",
        json_schema_extra={
            "placeholder": "payouts.list",
            "dynamicOptions": True,
            "loadOptionsMethod": "dodoPaymentsEndpoints",
            **_show("call"),
        },
    )
    request: Dict[str, Any] = Field(
        default_factory=dict,
        description="The method's keyword arguments as a JSON object, with the API's own names",
        json_schema_extra={"editor": "json", "rows": 6, **_show("call")},
    )
    search: str = Field(
        default="",
        description="Case-insensitive substring over endpoint names and routes",
        json_schema_extra={"placeholder": "refund", **_show("list_operations")},
    )

    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, values: Any) -> Any:
        # The panel stores "" for cleared fields and renders `request` as
        # text; an LLM may also stringify the JSON object.
        return coerce_blank_params(cls, values, object_fields=("request",))


class DodoPaymentsActionOutput(BaseModel):
    operation: Optional[str] = None
    mode: Optional[str] = None
    endpoint: Optional[str] = None
    result: Optional[Any] = None
    count: Optional[int] = None
    page: Optional[int] = None
    page_size: Optional[int] = None

    model_config = ConfigDict(extra="allow")


def _changes_anything(data: Mapping[str, Any]) -> bool:
    """Of the held operations, `call` waits for the owner only when its
    endpoint writes (an unknown one counts as writing)."""
    if str(data.get("operation") or "") != "call":
        return True
    return _sdk.endpoint_writes(str(data.get("endpoint") or ""))


class DodoPaymentsActionNode(ActionNode):
    type = "dodoPaymentsAction"
    display_name = "Dodo Payments"
    subtitle = "Payments & Billing"
    group = ("payments", "tool")
    description = (
        "Dodo Payments through the official Python SDK: payments, subscriptions, customers, refunds, "
        "checkout links, or any API endpoint"
    )
    component_kind = "square"
    tool_name = "dodo_payments_action"
    tool_description = (
        "Work with Dodo Payments through its official Python SDK, using the API key saved in Credentials -> "
        "Dodo Payments. The key decides the mode: every result carries mode (test_mode or live_mode). Amounts are "
        "integers in the currency's smallest unit (cents). Reads: list_payments, list_subscriptions, list_customers, "
        "list_products, list_refunds, list_disputes (one page each: page from 0, page_size up to 100; a full page "
        "may have more), and get_payment / get_subscription / get_customer / get_product by id. "
        "create_checkout_session (product_id, quantity; customer_id, or customer_email with customer_name; optional "
        "return_url and discount_code) returns a checkout_url to send the customer (it works once, for 24 hours). "
        "create_customer (customer_email and customer_name). create_customer_portal_link (customer_id) returns a "
        "link where the customer manages their billing. create_refund (payment_id; refunds the whole payment, or "
        "one item with product_id, or part of it with product_id and amount; optional reason), cancel_subscription "
        "(subscription_id; cancel_at_next_billing_date keeps it until the paid period ends) and "
        "change_subscription_plan (subscription_id, product_id, quantity, proration_billing_mode) move money and "
        "may wait for the owner's approval. call reaches any other SDK method: endpoint is its dotted name (e.g. "
        "payouts.list, discounts.create, subscriptions.charge), request its keyword arguments as a JSON object; "
        "list_operations (optional search) lists every endpoint with its HTTP route and parameters and needs no "
        "credentials. Reference: https://docs.dodopayments.com/api-reference/introduction"
    )
    handles = (
        {"name": "input-main", "kind": "input", "position": "left", "label": "Input", "role": "main"},
        {"name": "output-main", "kind": "output", "position": "right", "label": "Output", "role": "main"},
    )
    # Refunds, cancellations, plan changes and `call` change real accounts.
    annotations = {"destructive": True, "readonly": False, "open_world": True}
    credentials = (DodoPaymentsCredential,)
    task_queue = TaskQueue.REST_API
    usable_as_tool = True
    # usable_as_tool auto-sets both hide flags unless declared. This node
    # stays wirable on the canvas through its declared `handles`; the
    # frontend reads the flags only for a spec with no handles.
    hide_input_handle = False
    hide_output_handle = False
    approval = ApprovalSpec(
        channel="Dodo Payments",
        action="Make a change in Dodo Payments",
        operations=_HELD_OPERATIONS,
        when=_changes_anything,
        recipient=("customer_id", "customer_email"),
        recipient_otherwise="the customer",
        details=(
            ("Operation", "operation"),
            ("Payment", "payment_id"),
            ("Subscription", "subscription_id"),
            ("Product", "product_id"),
            ("Amount", "amount"),
            ("Reason", "reason"),
            ("Endpoint", "endpoint"),
        ),
        outcome_labels=("Done", "Not done"),
    )

    Params = DodoPaymentsActionParams
    Output = DodoPaymentsActionOutput

    # ---- shared plumbing -------------------------------------------------

    @staticmethod
    def _shape(operation: str, **fields: Any) -> Dict[str, Any]:
        """``{"operation": ...}`` plus every field that is not ``None``. An empty
        ``[]`` / ``{}`` result is a real answer from Dodo and is kept."""
        shaped: Dict[str, Any] = {"operation": operation}
        shaped.update({k: v for k, v in fields.items() if v is not None})
        return shaped

    @staticmethod
    def _required(value: str, field: str, operation: str, example: str) -> str:
        value = value.strip()
        if not value:
            raise NodeUserError(f"{field} is required for {operation} (e.g. {example})")
        return value

    async def _run(self, ctx: NodeContext, endpoint: str, request: Mapping[str, Any], *, writes: bool) -> Tuple[Any, str]:
        """One SDK call, against the mode of the stored key. The credential is
        resolved first, so a missing key surfaces as the
        ``PermissionDeniedError`` envelope that points at the modal."""
        secrets = await DodoPaymentsCredential.resolve(user_id=ctx.credential_customer_id)
        mode = stored_mode(secrets)
        async with _sdk.open_client(
            secrets["api_key"],
            mode,
            max_retries=0 if writes else _sdk.READ_RETRIES,
            timeout=_sdk.CALL_TIMEOUT_SECONDS,
        ) as client:
            return await _sdk.invoke(client, endpoint, request, writes=writes), mode

    async def _list(self, ctx: NodeContext, operation: str, endpoint: str, params: DodoPaymentsActionParams, **filters: Any) -> Any:
        request = {"page_number": params.page, "page_size": params.page_size, **_given(filters)}
        page, mode = await self._run(ctx, endpoint, request, writes=False)
        items = page.get("items") or []
        return self._shape(operation, mode=mode, result=items, count=len(items), page=params.page, page_size=params.page_size)

    async def _one(self, ctx: NodeContext, operation: str, endpoint: str, request: Mapping[str, Any], *, writes: bool) -> Any:
        result, mode = await self._run(ctx, endpoint, request, writes=writes)
        return self._shape(operation, mode=mode, result=result)

    @staticmethod
    def _status(value: str) -> Optional[str]:
        return None if value == "all" else value

    # ---- reads -------------------------------------------------------------

    @Operation("list_payments")
    async def list_payments(self, ctx: NodeContext, params: DodoPaymentsActionParams) -> Any:
        return await self._list(
            ctx, "list_payments", "payments.list", params,
            customer_id=params.customer_id.strip(),
            subscription_id=params.subscription_id.strip(),
            product_id=params.product_id.strip(),
            status=self._status(params.payment_status),
        )

    @Operation("get_payment")
    async def get_payment(self, ctx: NodeContext, params: DodoPaymentsActionParams) -> Any:
        payment_id = self._required(params.payment_id, "payment_id", "get_payment", "pay_...")
        return await self._one(ctx, "get_payment", "payments.retrieve", {"payment_id": payment_id}, writes=False)

    @Operation("list_subscriptions")
    async def list_subscriptions(self, ctx: NodeContext, params: DodoPaymentsActionParams) -> Any:
        return await self._list(
            ctx, "list_subscriptions", "subscriptions.list", params,
            customer_id=params.customer_id.strip(),
            product_id=params.product_id.strip(),
            status=self._status(params.subscription_status),
        )

    @Operation("get_subscription")
    async def get_subscription(self, ctx: NodeContext, params: DodoPaymentsActionParams) -> Any:
        subscription_id = self._required(params.subscription_id, "subscription_id", "get_subscription", "sub_...")
        return await self._one(
            ctx, "get_subscription", "subscriptions.retrieve", {"subscription_id": subscription_id}, writes=False
        )

    @Operation("list_customers")
    async def list_customers(self, ctx: NodeContext, params: DodoPaymentsActionParams) -> Any:
        return await self._list(ctx, "list_customers", "customers.list", params, email=params.customer_email.strip())

    @Operation("get_customer")
    async def get_customer(self, ctx: NodeContext, params: DodoPaymentsActionParams) -> Any:
        customer_id = self._required(params.customer_id, "customer_id", "get_customer", "cus_...")
        return await self._one(ctx, "get_customer", "customers.retrieve", {"customer_id": customer_id}, writes=False)

    @Operation("list_products")
    async def list_products(self, ctx: NodeContext, params: DodoPaymentsActionParams) -> Any:
        return await self._list(ctx, "list_products", "products.list", params)

    @Operation("get_product")
    async def get_product(self, ctx: NodeContext, params: DodoPaymentsActionParams) -> Any:
        product_id = self._required(params.product_id, "product_id", "get_product", "pdt_...")
        return await self._one(ctx, "get_product", "products.retrieve", {"id": product_id}, writes=False)

    @Operation("list_refunds")
    async def list_refunds(self, ctx: NodeContext, params: DodoPaymentsActionParams) -> Any:
        return await self._list(
            ctx, "list_refunds", "refunds.list", params,
            customer_id=params.customer_id.strip(),
            subscription_id=params.subscription_id.strip(),
            status=self._status(params.refund_status),
        )

    @Operation("list_disputes")
    async def list_disputes(self, ctx: NodeContext, params: DodoPaymentsActionParams) -> Any:
        return await self._list(ctx, "list_disputes", "disputes.list", params, customer_id=params.customer_id.strip())

    # ---- writes ------------------------------------------------------------

    @Operation("create_checkout_session")
    async def create_checkout_session(self, ctx: NodeContext, params: DodoPaymentsActionParams) -> Any:
        product_id = self._required(params.product_id, "product_id", "create_checkout_session", "pdt_...")
        customer: Optional[Dict[str, str]] = None
        if params.customer_id.strip():
            customer = {"customer_id": params.customer_id.strip()}
        elif params.customer_email.strip():
            customer = {"email": params.customer_email.strip(), **_given({"name": params.customer_name.strip()})}
        discount_code = params.discount_code.strip()
        request = {
            "product_cart": [{"product_id": product_id, "quantity": params.quantity}],
            **_given({
                "customer": customer,
                "return_url": params.return_url.strip(),
                "discount_codes": [discount_code] if discount_code else None,
            }),
        }
        return await self._one(ctx, "create_checkout_session", "checkout_sessions.create", request, writes=True)

    @Operation("create_customer")
    async def create_customer(self, ctx: NodeContext, params: DodoPaymentsActionParams) -> Any:
        email = self._required(params.customer_email, "customer_email", "create_customer", "ada@example.com")
        name = self._required(params.customer_name, "customer_name", "create_customer", "Ada Lovelace")
        return await self._one(ctx, "create_customer", "customers.create", {"email": email, "name": name}, writes=True)

    @Operation("create_customer_portal_link")
    async def create_customer_portal_link(self, ctx: NodeContext, params: DodoPaymentsActionParams) -> Any:
        customer_id = self._required(params.customer_id, "customer_id", "create_customer_portal_link", "cus_...")
        # send_email off: the link comes back to the caller, and Dodo emails no one.
        request = {"customer_id": customer_id, "send_email": False, **_given({"return_url": params.return_url.strip()})}
        return await self._one(
            ctx, "create_customer_portal_link", "customers.customer_portal.create", request, writes=True
        )

    @Operation("create_refund")
    async def create_refund(self, ctx: NodeContext, params: DodoPaymentsActionParams) -> Any:
        payment_id = self._required(params.payment_id, "payment_id", "create_refund", "pay_...")
        product_id = params.product_id.strip()
        if params.amount is not None and not product_id:
            raise NodeUserError(
                "A partial refund names the item it refunds: set product_id with amount, "
                "or leave amount empty to refund the whole payment."
            )
        request: Dict[str, Any] = {"payment_id": payment_id, **_given({"reason": params.reason.strip()})}
        if product_id:
            request["items"] = [{"item_id": product_id, **_given({"amount": params.amount})}]
        return await self._one(ctx, "create_refund", "refunds.create", request, writes=True)

    @Operation("cancel_subscription")
    async def cancel_subscription(self, ctx: NodeContext, params: DodoPaymentsActionParams) -> Any:
        subscription_id = self._required(params.subscription_id, "subscription_id", "cancel_subscription", "sub_...")
        change = {"cancel_at_next_billing_date": True} if params.cancel_at_next_billing_date else {"status": "cancelled"}
        return await self._one(
            ctx, "cancel_subscription", "subscriptions.update", {"subscription_id": subscription_id, **change}, writes=True
        )

    @Operation("change_subscription_plan")
    async def change_subscription_plan(self, ctx: NodeContext, params: DodoPaymentsActionParams) -> Any:
        subscription_id = self._required(params.subscription_id, "subscription_id", "change_subscription_plan", "sub_...")
        product_id = self._required(params.product_id, "product_id", "change_subscription_plan", "pdt_...")
        request = {
            "subscription_id": subscription_id,
            "product_id": product_id,
            "quantity": params.quantity,
            "proration_billing_mode": params.proration_billing_mode,
        }
        return await self._one(ctx, "change_subscription_plan", "subscriptions.change_plan", request, writes=True)

    # ---- any endpoint --------------------------------------------------------

    @Operation("call")
    async def call(self, ctx: NodeContext, params: DodoPaymentsActionParams) -> Any:
        # Resolved and checked before any credential or network use.
        info = await asyncio.to_thread(_sdk.resolve_endpoint, params.endpoint)
        request = _sdk.check_request(info, params.request)
        result, mode = await self._run(ctx, info.endpoint, request, writes=not info.reads)
        return self._shape("call", mode=mode, endpoint=info.endpoint, result=result)

    @Operation("list_operations")
    async def list_operations(self, ctx: NodeContext, params: DodoPaymentsActionParams) -> Any:
        # Pure introspection of the installed SDK: no credentials, no network.
        index = await asyncio.to_thread(_sdk.endpoint_index)
        needle = params.search.strip().lower()
        matches = [
            info.as_dict() for info in index if not needle or needle in info.endpoint.lower() or needle in info.path.lower()
        ]
        return self._shape("list_operations", result=matches, count=len(matches))


__all__ = ["DodoPaymentsActionNode", "DodoPaymentsActionOutput", "DodoPaymentsActionParams"]
