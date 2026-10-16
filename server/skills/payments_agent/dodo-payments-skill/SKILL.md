---
name: dodo-payments-skill
description: Take payments and manage billing in Dodo Payments through its official SDK — look up payments, subscriptions, customers, products, refunds and disputes, send checkout and customer-portal links, refund payments, cancel subscriptions, change plans, and call any other Dodo API endpoint. Output is Dodo's own JSON.
allowed-tools: "dodo_payments_action"
metadata:
  author: opencompany
  version: "1.0"
  category: payments

---

# Dodo Payments Skill

Wrapper over the official [Dodo Payments Python SDK](https://docs.dodopayments.com/developer-resources/sdks/python).
Typed operations cover the everyday flows; `call` reaches every other
endpoint of the API and `list_operations` discovers them. Results are
Dodo's own response fields.

## Tool: dodo_payments_action

### Operations

| Operation | Purpose | Key fields |
|---|---|---|
| `list_payments` | Payments | `customer_id`, `subscription_id`, `product_id`, `payment_status`, `page`, `page_size` |
| `get_payment` | One payment: status, amounts, customer, refunds, disputes, invoice URL | `payment_id` (required) |
| `list_subscriptions` | Subscriptions | `customer_id`, `product_id`, `subscription_status`, `page`, `page_size` |
| `get_subscription` | One subscription: status, plan, next billing date | `subscription_id` (required) |
| `list_customers` | Customers | `customer_email` (filter), `page`, `page_size` |
| `get_customer` | One customer | `customer_id` (required) |
| `list_products` / `get_product` | The catalogue, or one product with its price | `product_id` for `get_product` |
| `list_refunds` | Refunds | `customer_id`, `subscription_id`, `refund_status` |
| `list_disputes` | Disputes and chargebacks | `customer_id` |
| `create_checkout_session` | A checkout link to send the customer | `product_id` (required), `quantity`, `customer_id` or `customer_email` + `customer_name`, `return_url`, `discount_code` |
| `create_customer` | A new customer | `customer_email`, `customer_name` (both required) |
| `create_customer_portal_link` | A link where the customer updates their card, sees invoices or cancels | `customer_id` (required), `return_url` |
| `create_refund` | Refund a payment | `payment_id` (required); `product_id` refunds one item, with `amount` part of it; `reason` |
| `cancel_subscription` | Cancel now, or at the end of the paid period | `subscription_id` (required), `cancel_at_next_billing_date` |
| `change_subscription_plan` | Move a subscription to another product | `subscription_id`, `product_id` (required), `quantity`, `proration_billing_mode` |
| `call` | Any other SDK method | `endpoint` (required), `request` |
| `list_operations` | Every endpoint, its HTTP route and parameters | `search` |

Amounts are integers in the currency's smallest unit: `900` is $9.00.

### Response

```json
{
  "operation": "list_payments",
  "mode": "test_mode",
  "result": [
    { "payment_id": "pay_123", "status": "succeeded", "total_amount": 900, "currency": "USD", "customer": { "email": "ada@example.com" } }
  ],
  "count": 1,
  "page": 0,
  "page_size": 20
}
```

`mode` says which Dodo environment answered: `test_mode` (no real money)
or `live_mode`. Lists return one page; when `count` equals `page_size`
there may be more, so ask for the next `page` (pages start at 0).

## Typical flows

Sell something — find the product, then send the link:

```json
{ "operation": "list_products" }
{ "operation": "create_checkout_session", "product_id": "pdt_123", "customer_email": "ada@example.com", "customer_name": "Ada Lovelace", "return_url": "https://example.com/thanks" }
```

The answer carries `checkout_url`. Send it to the customer with a
messaging tool; it works once, for 24 hours.

Refund the latest payment of a customer:

```json
{ "operation": "list_customers", "customer_email": "ada@example.com" }
{ "operation": "list_payments", "customer_id": "cus_123", "payment_status": "succeeded", "page_size": 1 }
{ "operation": "create_refund", "payment_id": "pay_123", "reason": "Customer asked for a refund" }
```

A partial refund names the item and the amount:
`{ "operation": "create_refund", "payment_id": "pay_123", "product_id": "pdt_123", "amount": 500 }`.

Cancel a subscription at the end of the period it is paid for:

```json
{ "operation": "cancel_subscription", "subscription_id": "sub_123", "cancel_at_next_billing_date": true }
```

Upgrade a plan, charging only the prorated difference now:

```json
{ "operation": "change_subscription_plan", "subscription_id": "sub_123", "product_id": "pdt_pro", "quantity": 1, "proration_billing_mode": "prorated_immediately" }
```

`subscriptions.preview_change_plan` (through `call`) shows the charge first
without changing anything.

## The full API via call

Discover, then call. `list_operations` needs no credentials and makes no
request:

```json
{ "operation": "list_operations", "search": "payout" }
```

Each entry lists the `http_method`, `path` and `params` (with `required`).
Then call the SDK method by its dotted name, with its keyword arguments in
`request`:

```json
{ "operation": "call", "endpoint": "payouts.list", "request": { "page_size": 10 } }
{ "operation": "call", "endpoint": "payments.retrieve_line_items", "request": { "payment_id": "pay_123" } }
{ "operation": "call", "endpoint": "discounts.create", "request": { "type": "percentage", "amount": 2000, "code": "SPRING20" } }
{ "operation": "call", "endpoint": "subscriptions.charge", "request": { "subscription_id": "sub_123", "product_price": 1500 } }
{ "operation": "call", "endpoint": "licenses.validate", "request": { "license_key": "LK-..." } }
```

Notes for `call`:

- An unknown endpoint or parameter fails before any request, with the
  closest names or the accepted parameters.
- A discount `amount` is in basis points: `2000` is 20%.
- List endpoints answer with one page as `{ "items": [...] }`; pass
  `page_number` and `page_size` in `request` for others.
- Endpoints that answer with a file (invoice PDFs) are not supported.
- Webhook endpoints and secrets are not reachable: they stay in the Dodo
  dashboard.

## Approvals

While the owner asks first, `create_refund`, `cancel_subscription`,
`change_subscription_plan`, and any `call` that changes something (every
method but a GET), wait for the owner's approval instead of running. Such
a call answers with `status: waiting_for_owner`: tell the user it is
waiting for their approval and do not repeat it. Reads, checkout links,
new customers and portal links run straight away.

## Errors

Errors carry Dodo's own code and message, for example
`payments.retrieve failed (404 NOT_FOUND): Payment not found`. Surface them
verbatim.

- `401` — the key was rejected: point the user at Credentials -> Dodo
  Payments.
- `403` — a read-only key cannot write, or the business is not live yet.
- "The change may or may not have been made" — a write timed out or Dodo
  had a server error. Look the payment, refund or subscription up before
  trying again: writes are never retried automatically, because a repeated
  refund would refund twice.

## Webhooks (`dodoPaymentsReceive` trigger)

Dodo posts events to `https://<OpenCompany address>/webhook/dodo-payments`
once an endpoint is added in the Dodo dashboard (Developer -> Webhooks) and
its signing secret is saved in Credentials -> Dodo Payments. Every delivery
is verified (Standard Webhooks signature, refused when more than five
minutes old) and fires the trigger with:

`event_id`, `event_type` (`payment.succeeded`, `refund.failed`,
`subscription.renewed`, `dispute.opened`, ...), `business_id`, `timestamp`,
`payload_type` (`Payment`, `Subscription`, `Refund`, `Dispute`, ...) and
`data`, the payment, subscription, refund or dispute itself.

The trigger's `event_type_filter` takes `all`, an exact type such as
`payment.succeeded`, or a prefix such as `subscription.*`.

## Authentication

The tool uses the API key saved in **Credentials -> Dodo Payments**.
Validation finds whether it is a test-mode or a live-mode key, and every
call goes to that mode. Never ask the user to paste a key in chat.

## Best practices

1. **Check `mode`** before acting on what you read: test data is not real
   money. Say so when you work in live mode and the intent is exploratory.
2. **Confirm money moves** (refunds, cancellations, plan changes, charges)
   with the user unless they asked for exactly that.
3. **Prefer filters to long listings**: `customer_id`, a status filter, or
   a smaller `page_size`.
4. **Surface Dodo's error messages verbatim**; they name the exact fix.
