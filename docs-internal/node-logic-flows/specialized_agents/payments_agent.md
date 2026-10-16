# Payments Agent (`payments_agent`)

| Field | Value |
|------|-------|
| **Category** | specialized_agents |
| **Plugin** | [`server/nodes/agent/payments_agent/__init__.py`](../../../server/nodes/agent/payments_agent/__init__.py) -> [`_specialized.py::SpecializedAgentBase.execute_op`](../../../server/nodes/agent/_specialized.py) (dispatch via `BaseNode.execute()`) |
| **Theme color** | `dracula.green` |
| **Icon** | credit card (U+1F4B3) |
| **Tests** | [`server/tests/nodes/test_specialized_agents.py`](../../../server/tests/nodes/test_specialized_agents.py) |

## Purpose

AI agent pre-configured for payment processing and financial workflows. The
payment providers plug in as ordinary tools on its `input-tools` handle;
there is no provider registry on the agent itself.

## What is unique to this node

- **Intended tool set**: the first-party payment tools
  [`stripeAction`](../stripe/stripeAction.md) (Stripe CLI) and
  [`dodoPaymentsAction`](../dodo_payments/dodoPaymentsAction.md) (Dodo
  Payments SDK), or `httpRequest` against any other payment API.
- **Intended skills**: `server/skills/payments_agent/` ships `stripe-skill`
  and `dodo-payments-skill`, attached automatically when their tool is wired
  to an agent (`nodes/visuals.json`). The node itself is a naming / theming
  shell around `SpecializedAgentBase` -> `execute_chat_agent`.
- **Frontend theming**: green dracula accent.

## Behaviour

See **[Generic Specialized Agent Pattern](./_pattern.md)**.

## Related

- **Pattern doc**: [`_pattern.md`](./_pattern.md)
