# RFC-0003 — OpenAI-Compatible Provider Contract

Status: Draft
Created: 2026-09-01
Scope: `server/services/llm/`, `server/nodes/model/`
Relationship: independent of RFC-0002 (Context/Memory). Touches no agent state.

## How to read this

If you are **adding a provider**, read §9. If you are **implementing**, read §3
(decisions) then §10 (phases). If you are **reviewing**, §3 and §11 are the
whole normative surface; everything else is rationale.

Files this RFC changes, by phase:

| File | Phase | Change |
|---|---|---|
| `services/llm/protocol.py` | 1 | `LLMError` gains `public_message`; new `PROTOCOL` category |
| `services/llm/config.py` | 1 | `resolve_credential()`; capability accessors |
| `services/llm/providers/openai.py` | 2 | drop `api_key="ollama"`; drop `proxy_url` param |
| `services/llm/providers/anthropic.py` | 2 | drop the duplicate placeholder hardcode |
| `services/llm/unifier.py` | 2 | collapse proxy/base into one effective URL + `url_source` |
| `services/llm/endpoints.py` | 3 | **new** — save-time base-URL resolver |
| `nodes/model/_local_validator.py` | 3 | resolve before persist; drop the `/v1` 404 hint |
| `config/llm_defaults.json` | 1, 4 | `auth` block; capability flags |
| `services/llm/providers/openai.py` | 5 | 2xx-with-error-body guard |
| `docs-internal/native_llm_sdk.md` | 1 | fix the DeepSeek `/v1` claim (§13) |

## 1. Summary

OpenCompany treats "OpenAI-compatible" as one shape. It is a family of
divergent shapes with **no specification behind it**, and the divergences are
invisible from a successful response.

There are only two ways to hold a per-provider fact: declare it, or guess it.
Today the client guesses — that a stored base URL is correctly rooted, that the
credential belongs in `Authorization: Bearer`, that a provider with a
`{provider}_proxy` row wants the literal key `"ollama"`, and that HTTP 200 means
the request was understood. It also validates two providers against a different
wire surface than the one execution uses, so a credential can read green while
the runtime is dead.

This RFC makes the endpoint and the credential **resolved facts** — computed
once, at save time, with evidence — and makes capability variance **declared**,
extending the `supports_model_listing` precedent the repo already ships. The
error contract is handled behaviourally, not by declaration (§8.4).

## 2. Problem

### 2.1 The failure chain (normative; other sections cross-reference here)

A stored LM Studio base URL without `/v1`. The panel read **Connected** with
models discovered. Every run failed with `AI generated empty response`.

| # | Step | Site |
|---|---|---|
| 1 | URL persisted verbatim, **before** the probe runs | `_local_validator.py:270-275` |
| 2 | Probe strips a *trailing* `/v1` and the scheme, then talks to LM Studio's **native WebSocket SDK**. With no `/v1` present the strip is a no-op, so the probe cannot see the defect | `_local_validator.py:124-134`, `:201-203` |
| 3 | Probe succeeds. `valid=True` broadcast | `_local_validator.py:314-339` |
| 4 | Runtime reads the same row, for every provider | `unifier.py:296` |
| 5 | `url = proxy_url or base_url` then `AsyncOpenAI(base_url=url)`, key overwritten with `"ollama"` | `providers/openai.py:45-49` |
| 6 | SDK concatenates prefix + endpoint, giving `POST /chat/completions` | `openai/_base_client.py:496` |
| 7 | LM Studio answers **HTTP 200** with an `{"error": ...}` body | vendor behaviour |
| 8 | SDK raises only on 4xx/5xx. `choices` is absent, so it coerces to `None` | `openai/_models.py` |
| 9 | Empty content becomes `AI generated empty response` | `services/ai.py` |

Two structural defects compound. **Validation exercises a surface execution
never touches** — LM Studio serves OpenAI-compat at `/v1/*`, native REST at
`/api/v1/*`, and a WebSocket SDK at `ws://{host}/{namespace}`, all on one port;
Ollama likewise serves `/api/*` beside `/v1/*`. A green Fetch proves host
liveness on the *native* surface and nothing about the prefix the runtime uses.
And **a 2xx is treated as an understood request**, which for this class of
server it is not.

### 2.2 Why the endpoint cannot be inferred

`base_url` is a path **prefix**, string-concatenated with the endpoint
(`_prepare_url`: `base_url.raw_path + endpoint.lstrip("/")`,
`openai/_base_client.py:496`) — not `urljoin`. The base path is never
discarded, and the SDK **never inserts `/v1`**; that segment lives in the
default constant `https://api.openai.com/v1`. Override `base_url` and the whole
prefix is yours.

`/v1` is neither universal nor positionally predictable:

| Shape | Providers |
|---|---|
| Root-mounted, versionless | DeepSeek (`/chat/completions` at root; siblings `/beta`, `/anthropic`) |
| Namespaced | Groq `/openai/v1`, Fireworks `/inference/v1`, OpenRouter `/api/v1` |
| Version varies by model | Sarvam `/v1` and `/v2` |
| `/v1` only | vLLM, Ollama, LM Studio, Mistral, xAI, Kimi, Cerebras, Together |
| Both root and `/v1` | llama.cpp, LocalAI, LiteLLM |
| Configurable, may be empty | Jan |

So "append `/v1` when the URL has no path" is wrong for the first three rows,
and it fails in the opposite direction for path-prefix deployments — vLLM
`--root-path`, llama.cpp `--api-prefix`, Open WebUI proxying Ollama under
`/ollama` — where a URL that *has* a path still needs `/v1` appended.

**A path-less base URL is genuinely ambiguous**: indistinguishable from a
deliberately root-mounted server without probing. No string rule resolves it.
That is why §6 probes.

`AzureOpenAI` is the shape of the fix: it takes `azure_endpoint` (host, no
path), builds `f"{endpoint}/openai"` itself, and rejects `base_url` as mutually
exclusive. Where the prefix is knowable, the library supplies it.

## 3. Decisions

| # | Decision | Locked by |
|---|---|---|
| D1 | A configured provider's `base_url` is copied verbatim from vendor docs. The runtime never appends, strips, or rewrites a path segment. | AG1, AG2 |
| D2 | A user-supplied base URL is **resolved by probing at save time**, once, and the resolved value is persisted. The runtime consumes it verbatim. | AG3, AG4 |
| D3 | Probe order is `{base}/models` then `{base}/v1/models`. First success wins. 401/403 counts as rooted. | AG3 |
| D4 | The probe verdict is three-state: `verified`, `rooted-unverified`, `failed`. | AG5 |
| D5 | On probe failure nothing is written to `{provider}_proxy`. The verdict lands on the credential row. | AG6 |
| D6 | The credential is resolved by one function. No provider hardcodes a placeholder key. | AG7, AG8 |
| D7 | No code path hands the SDK an unresolved credential — `api_key=None` makes the SDK read `OPENAI_API_KEY` and ship the operator's OpenAI key to a third party. | AG8 |
| D8 | A capability is **declared**, never sniffed. A flag ships only with a v1 consumer. | AG9 |
| D9 | A 2xx chat response with no `choices` and an error-shaped body raises `PROTOCOL`, naming the URL called. | AG10 |
| D10 | Every provider-call failure logs the effective URL and `url_source`, redacted. Never the key. | AG11 |
| D11 | `ProviderSpec` gains no field. | AG12 |
| D12 | `_strip_v1_path` survives, scoped to the native enrichment probe only. | AG2 |

## 4. Goals and non-goals

**Goals.** One connected verdict across panel and palette. A stored URL that
works keeps working with no operator action. A routing mistake is
distinguishable from a model failure in the message the user sees. Adding a
compatible provider is a JSON edit plus one name in a tuple.

**Non-goals.** Not a universal LLM abstraction — `ChatUnifier` stays the single
facade. Not a provider-registry rewrite. Not Anthropic's or Gemini's native wire
format; D6 and D7 apply to their credential handling only, because they share
the defect. Not per-model *discovery* — per-model declaration is permitted via
the existing `CapabilityConfig` precedence and required only where vendor docs
document a divergence.

## 5. The base URL contract

A provider's `base_url` in `llm_defaults.json` is an **opaque, vendor-declared,
verbatim prefix**. It is copied from the vendor's own documentation and is never
synthesized, completed, or normalized by the runtime.

This is why `deepseek: "https://api.deepseek.com"` is **correct** — DeepSeek
documents exactly that, with routes at `/chat/completions`. It is not a missing
`/v1` (§13).

Trailing slashes are irrelevant: `_enforce_trailing_slash`
(`openai/_base_client.py:414`) normalizes the base on assignment. Defensive
stripping or appending in our code is dead weight.

## 6. Endpoint resolution

Applies **only** to user-supplied URLs for self-hosted providers (`ollama`,
`lmstudio`). Configured providers are covered by §5.

```python
# services/llm/endpoints.py -- save-time only. Async, does IO.
async def resolve_base_url(candidate: str, *, api_key: str) -> ResolvedBaseUrl:
    """Probe which prefix actually roots the OpenAI surface.

    A path-less URL is ambiguous: LiteLLM/llama.cpp/Jan serve at root,
    LM Studio/Ollama/vLLM only under /v1. Probing is the only way to tell.

    GET /models is the one endpoint every compatible server implements and
    the only side-effect-free one -- /chat/completions loads a model and
    bills. Where a provider declares model_listing.path: null (Sarvam), the
    declared fallback is one max_tokens=1 completion; that fallback is
    itself a declared fact, not a guess.
    """
```

Order, first success wins:

| Candidate | Outcome |
|---|---|
| `{base}/models` returns 2xx | `verified`, adopt `{base}` |
| `{base}/models` returns 401/403 | `rooted-unverified`, adopt `{base}` |
| `{base}/v1/models` returns 2xx | `verified`, adopt `{base}/v1`, `rewritten=True` |
| `{base}/v1/models` returns 401/403 | `rooted-unverified`, adopt `{base}/v1`, `rewritten=True` |
| neither | `failed`; nothing persisted to `{provider}_proxy` (D5) |

401/403 means **routing succeeded, credentials did not** — vLLM's `--api-key`
protects the whole `/v1` prefix. At probe time that is a success signal. At
runtime the same status is a failure (§8.3). The two rules are deliberately
different, and both are stated here so neither is mistaken for the other.

This handles path-prefix deployments correctly: `https://host/vllm` probes
`/vllm/models` then `/vllm/v1/models`. A has-a-path heuristic gets that
backwards.

`rewritten=True` logs at INFO naming both candidates. An operator who typed one
thing and got another must be able to find out why.

**Two layers, different rules.** Save-time resolution is async and does IO.
Call-time consumption is synchronous and pure — it reads the resolved value and
nothing else. The AG1 tripwire excludes `endpoints.py` for exactly this reason,
and that exclusion is the module's justification for existing.

## 7. Credential resolution

`OpenAIProvider.__init__` currently does `if proxy_url: kwargs["api_key"] =
"ollama"` (`providers/openai.py:49`), duplicated at `anthropic.py:41`. That
hardcodes a dummy key for **any** provider with a proxy row, not just local
ones, and it is why a cloud proxy cannot work today.

It is replaced by one resolver, in `config.py`:

```python
def resolve_credential(provider: str, stored: str | None) -> str:
    """Return the key to send. Never None, never a hardcoded vendor name."""
```

Providers that require a placeholder declare it:

```json
"lmstudio": { "auth": { "scheme": "bearer", "placeholder_key": "lm-studio" } }
```

Ollama's own docs say `api_key='ollama'` is "required but ignored", so the
placeholder is a **declared vendor fact**, not an invention.

**D7 is a security rule, not hygiene.** `AsyncOpenAI(api_key=None)` falls back
to `os.environ["OPENAI_API_KEY"]`. Combined with a third-party `base_url`, that
ships the operator's OpenAI key to another vendor. No path may reach the SDK
with an unresolved credential; AG8 asserts zero requests are issued in that
state.

## 8. Capabilities and the error contract

### 8.1 Declared, never sniffed

This extends `supports_model_listing` (`config.py:179-189`) rather than adding
a parallel mechanism. **A flag ships only with a consumer.** A declaration with
no reader is how `models_endpoint` and `build_headers` became dead config.

v1 flags and their consumers:

| Flag | Consumer |
|---|---|
| `model_listing.path` (null = no route) | existing `fetch_models` branch |
| `surface.responses` | replaces the `provider_name == "openai"` hardcode in `openai.py` |
| `auth.scheme`, `auth.placeholder_key` | `resolve_credential` (§7) |

Everything else — tool calling, structured outputs, embeddings, streaming usage
— is **declared next, not now**. Naming them here without a reader would repeat
the mistake this section exists to prevent.

### 8.2 The 2xx guard

A 2xx chat response with **no `choices`** and an error-shaped body is a routing
failure, not an empty completion. It raises `LLMErrorCategory.PROTOCOL` with a
message naming the URL actually called. This is the single change that would
have made the original bug self-diagnosing.

### 8.3 Runtime errors

401/403 at runtime is a credential failure and says so, quoting the effective
URL. Every failure logs `url_source` (one of `proxy`, `llm_defaults`,
`factory_default`), computed in `_get_or_create_entry` where the override is
chosen and carried into the exception. URLs pass through `redact_url()` — which
strips userinfo and the query string — before reaching a log or a user-facing
message. The key is never logged in any form.

Per the repo's `NodeUserError` contract, a routing or credential failure is
user-correctable: one WARN line, no traceback.

### 8.4 No declared error block

The four per-provider facts are rooting, auth, capability, and error contract.
The first three are declared; the **error contract is handled behaviourally** —
tolerant parsing plus the §8.2 guard. Declaring per-provider error shapes would
be a table with one entry and no way to keep it honest.

## 9. Adding an OpenAI-compatible provider

1. Copy `base_url` **verbatim** from the vendor's docs. Do not normalize it.
2. Add a `providers.<name>` block to `llm_defaults.json` with `base_url`, an
   `auth` block, and any capability flag that differs from the permissive
   default. Add a `_note` citing the doc URL for each non-default flag.
3. Add the name to `_COMPAT_PROVIDERS` in `providers/_compat.py`.
4. If the vendor documents no model-list route, set `model_listing.path: null`.
5. Nothing else. No provider subclass, no `ProviderSpec` field (D11), no Python.

## 10. Migration

Backward compatible throughout: an existing stored URL that works keeps working
with no operator action.

| Phase | Change | Rollback |
|---|---|---|
| 1 | `LLMError.public_message`, `PROTOCOL`, `resolve_credential`, `auth` blocks. Additive, no reader. | revert alone |
| 2 | Constructors consume `resolve_credential`; unifier collapses to one effective URL plus `url_source`. Touches `openai.py`, `anthropic.py`, `unifier.py`. | revert alone |
| 3 | `endpoints.py` plus validator resolves before persist. Gated by `LLM_ENDPOINT_CONTRACT_ENABLED`, shipping **false**; flipped in a separate commit. | flag |
| 4 | Capability flags with consumers (§8.1). | revert alone |
| 5 | 2xx guard. Depends on Phase 2 for `url_source`. | revert alone |

Phase 2 preserves behaviour exactly for every currently-working install: a local
provider with a placeholder row resolves to the same key it sends today. Phase 3
is where a previously-broken URL starts working.

## 11. Acceptance gates

| # | Assertion | Style |
|---|---|---|
| AG1 | No `"/v1"` literal and no `.rstrip("/")` under `services/llm/` or `nodes/model/`, except the AG2 allowlist | AST scan |
| AG2 | The allowlist is exactly `endpoints.py` (save-time resolver) and `_local_validator._strip_v1_path` (native probe only) | source introspection |
| AG3 | The resolver probes `{base}/models` then `{base}/v1/models`, adopting on 2xx or 401/403 | async unit |
| AG4 | Runtime client construction contains no probe and no `await` on a URL | AST |
| AG5 | All three verdict states render distinctly on both panel and palette | FE + BE unit |
| AG6 | A failed probe writes nothing to `{provider}_proxy` | async unit |
| AG7 | No literal `"ollama"` or `"lm-studio"` appears in any provider `__init__` | source scan |
| AG8 | With `OPENAI_API_KEY` set and no stored key, every provider raises `NodeUserError` and issues **zero** requests | async unit |
| AG9 | Every flag in `llm_defaults.json` has a reader in `services/llm/` | AST cross-check |
| AG10 | 2xx with no `choices` and an error body raises `PROTOCOL` naming the URL | unit |
| AG11 | No log line or `public_message` contains userinfo, a query string, or `Bearer` | unit |
| AG12 | The `ProviderSpec` field set is unchanged | source introspection |

## 12. What this RFC does not change

`ChatUnifier` stays the single facade. `ProviderSpec` keeps its shape (D11).
`sdk_exception_refs` stay lazy `"module:ClassName"` strings — no SDK import at
registration, per `tests/llm/test_lazy_sdk_imports.py`. `OpenRouterProvider`
keeps its own `__init__` and its hardcoded base; it is correct per vendor docs.
The dead JSON fields (`models_endpoint`, `api_key_format`, `extra_headers`) are
**not deleted here** — that is a subtractive change with its own risk, tracked
separately.

## 13. Compatibility audit of the nine compat providers

All nine `base_url` values in `llm_defaults.json` match their vendor docs and
need no change. In particular **`deepseek` is path-less and correct**.

The defect is in our documentation: `docs-internal/native_llm_sdk.md:235` states
DeepSeek's base as `https://api.deepseek.com/v1`, which the vendor does not
document. Phase 1 fixes the doc, not the config. Any future change that
"corrects" the JSON toward `/v1` is a regression.

## 14. References

- openai-python `_base_client.py` — `_prepare_url:496`, `_enforce_trailing_slash:414`
- openai-python `lib/azure.py` — `azure_endpoint` / `base_url` mutual exclusion
- DeepSeek https://api-docs.deepseek.com/ — base `https://api.deepseek.com`
- Groq https://console.groq.com/docs/openai — `/openai/v1`
- OpenRouter https://openrouter.ai/docs/quickstart — `/api/v1`
- Fireworks https://docs.fireworks.ai/tools-sdks/openai-compatibility — `/inference/v1`
- Sarvam https://docs.sarvam.ai/api/getting-started/models/open-source — `/v1` and `/v2`
- LM Studio https://lmstudio.ai/docs/app/api/endpoints/openai
- Ollama https://docs.ollama.com/api/openai-compatibility
- vLLM https://docs.vllm.ai/en/latest/cli/serve.html — `--root-path`
- llama.cpp https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md — `--api-prefix`
- LiteLLM https://docs.litellm.ai/docs/proxy/user_keys
- Open WebUI https://docs.openwebui.com/reference/api-endpoints/
