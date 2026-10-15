"""Everything that touches the ``dodopayments`` SDK, imported lazily.

The node never imports the SDK at module level: the plugin must register (and
the credentials modal must answer) even when it is missing. Every function
here that needs the SDK imports it inside its body.

Three jobs:

* **Clients**: :func:`open_client` builds an ``AsyncDodoPayments`` for one
  operation with the key and its mode's base URL passed explicitly, so no
  ``DODO_PAYMENTS_*`` variable in the server environment authenticates a node
  or points it elsewhere. A call that writes runs with the SDK's retries off:
  the SDK retries POSTs too, and Dodo's API takes no idempotency key, so a
  retried refund could be refunded twice.
* **Introspection** (no credentials, no network): :func:`endpoint_index`
  reflects every API call the installed SDK makes, with its HTTP route and
  parameters, for ``list_operations``, the endpoint dropdown, ``call`` and
  the approval rule. Nothing is vendored: an SDK upgrade is all it takes to
  pick up new endpoints.
* **Calls and shaping**: :func:`invoke` runs one SDK method, :func:`to_plain`
  returns the API's own JSON, and :func:`raise_user_error` maps the SDK's
  errors onto ``NodeUserError``.
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import functools
import inspect
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, AsyncIterator, Dict, List, Mapping, NoReturn, Tuple

from core.logging import get_logger
from services.plugin import NodeUserError

from ._credentials import MODES

logger = get_logger(__name__)

# The SDK's own default for calls that only read; calls that write get none.
READ_RETRIES = 2
# Per-request timeout for node calls (the SDK's default, made explicit).
CALL_TIMEOUT_SECONDS = 60.0
_BODY_PREVIEW_CHARS = 500
_MAX_SUGGESTIONS = 5
# Keyword arguments every SDK method takes besides the API's own parameters.
# extra_headers could replace the Authorization header, so neither the index
# nor `call` lets them through.
_SDK_KWARGS = frozenset({"extra_headers", "extra_query", "extra_body", "timeout"})
# Webhook endpoints and their signing secrets configure this integration
# itself, and a secret has no use in an agent's hands: they stay in the Dodo
# dashboard, out of the index and so out of `call`.
_EXCLUDED_RESOURCES = frozenset({"webhooks"})
# How a generated method makes its request: ``self._get(path_template("/x/{id}", ...``.
_ROUTE_RE = re.compile(r"self\._(get_api_list|get|post|patch|put|delete)\(\s*(?:path_template\(\s*)?[\"'](/[^\"']*)[\"']")


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def open_client(api_key: str, mode: str, *, max_retries: int, timeout: float) -> AsyncIterator[Any]:
    """An ``AsyncDodoPayments`` for one operation, closed afterwards."""
    try:
        from dodopayments import ENVIRONMENTS, AsyncDodoPayments
    except ImportError as exc:
        raise NodeUserError("The 'dodopayments' package is not installed. Run `uv sync` in server/.") from exc
    async with AsyncDodoPayments(
        bearer_token=api_key,
        base_url=ENVIRONMENTS[mode],
        max_retries=max_retries,
        timeout=timeout,
    ) as client:
        yield client


async def key_mode(api_key: str, *, timeout: float) -> str:
    """The mode a key belongs to. Each mode is asked to list the brands (every
    business has one), test first; a 401 means the key is not that mode's."""
    from dodopayments import APIError, AuthenticationError

    for mode in MODES:
        async with open_client(api_key, mode, max_retries=0, timeout=timeout) as client:
            try:
                await client.brands.list()
            except AuthenticationError:
                continue
            except APIError as exc:
                raise_user_error(exc, "brands.list", writes=False)
        return mode
    raise NodeUserError(
        "Dodo Payments rejected this API key in both test and live mode. "
        "Copy it again from Developer -> API Keys in the Dodo Payments dashboard."
    )


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParamInfo:
    name: str
    type: str
    required: bool


@dataclass(frozen=True)
class EndpointInfo:
    endpoint: str  # the SDK's dotted method, "customers.wallets.list"
    http_method: str  # "GET" / "POST" / "PATCH" / "PUT" / "DELETE"
    path: str  # "/customers/{customer_id}/wallets"
    params: Tuple[ParamInfo, ...]
    deprecated: bool

    @property
    def reads(self) -> bool:
        return self.http_method == "GET"

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self) | {"params": [asdict(p) for p in self.params], "reads": self.reads}


def _resources(owner: Any, resource_type: type) -> List[Tuple[str, Any]]:
    """The public resources hanging off a client or a resource: its cached
    properties that build an API resource (``with_raw_response`` and
    ``with_streaming_response`` are wrappers, not resources)."""
    found = []
    for name, member in inspect.getmembers(type(owner)):
        if name.startswith(("_", "with_")) or not isinstance(member, functools.cached_property):
            continue
        child = getattr(owner, name)
        if isinstance(child, resource_type):
            found.append((name, child))
    return found


def _describe(endpoint: str, fn: Any) -> EndpointInfo | None:
    """One method's route and parameters, or None for a local helper that
    makes no request (``webhooks.unwrap``)."""
    match = _ROUTE_RE.search(inspect.getsource(fn))
    if match is None:
        return None
    verb, path = match.groups()
    params = tuple(
        ParamInfo(
            name=p.name,
            type=p.annotation if isinstance(p.annotation, str) else getattr(p.annotation, "__name__", repr(p.annotation)),
            required=p.default is inspect.Parameter.empty,
        )
        for p in inspect.signature(fn).parameters.values()
        if p.name != "self" and p.name not in _SDK_KWARGS
    )
    return EndpointInfo(
        endpoint=endpoint,
        http_method="GET" if verb == "get_api_list" else verb.upper(),
        path=path,
        params=params,
        deprecated=getattr(fn, "__deprecated__", None) is not None,
    )


def _collect(path: str, resource: Any, resource_type: type, out: List[EndpointInfo]) -> None:
    for name, fn in inspect.getmembers(type(resource), inspect.isfunction):
        if name.startswith("_"):
            continue
        info = _describe(f"{path}.{name}", fn)
        if info is not None:
            out.append(info)
    for name, child in _resources(resource, resource_type):
        _collect(f"{path}.{name}", child, resource_type, out)


@functools.lru_cache(maxsize=1)
def endpoint_index() -> Tuple[EndpointInfo, ...]:
    """Every API call the installed SDK makes, by reflecting a throwaway
    client (the sync one, whose methods mirror the async client's). No
    network. Built once per process; async callers run it in a thread."""
    try:
        from dodopayments import DodoPayments
        from dodopayments._resource import SyncAPIResource
    except ImportError as exc:
        raise NodeUserError("The 'dodopayments' package is not installed. Run `uv sync` in server/.") from exc
    entries: List[EndpointInfo] = []
    with DodoPayments(bearer_token="introspection", base_url="http://introspection.invalid", max_retries=0) as client:
        for name, resource in _resources(client, SyncAPIResource):
            if name not in _EXCLUDED_RESOURCES:
                _collect(name, resource, SyncAPIResource, entries)
    entries.sort(key=lambda entry: entry.endpoint)
    logger.info("dodo payments endpoint index built", endpoints=len(entries))
    return tuple(entries)


@functools.lru_cache(maxsize=1)
def _by_endpoint() -> Dict[str, EndpointInfo]:
    return {entry.endpoint: entry for entry in endpoint_index()}


def resolve_endpoint(raw: str) -> EndpointInfo:
    """The index entry for a dotted endpoint name, with the closest names on
    a miss."""
    name = (raw or "").strip()
    if not name:
        raise NodeUserError(
            "endpoint is required (e.g. payouts.list). Use operation=list_operations to discover endpoints."
        )
    index = _by_endpoint()
    if name in index:
        return index[name]
    suggestions = difflib.get_close_matches(name, list(index), n=_MAX_SUGGESTIONS, cutoff=0.5)
    hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
    raise NodeUserError(
        f"Unknown Dodo Payments endpoint '{name}'.{hint} Use operation=list_operations to discover endpoints."
    )


def endpoint_writes(raw: str) -> bool:
    """Whether calling this endpoint changes anything: every method but GET.
    An unknown name counts as writing."""
    info = _by_endpoint().get((raw or "").strip())
    return info is None or not info.reads


def check_request(info: EndpointInfo, request: Mapping[str, Any]) -> Dict[str, Any]:
    """The request's keyword arguments, checked against the method's
    parameters before any network use."""
    problems = []
    unknown = sorted(set(request) - {p.name for p in info.params})
    if unknown:
        problems.append(f"unknown: {', '.join(unknown)}")
    missing = sorted(p.name for p in info.params if p.required and p.name not in request)
    if missing:
        problems.append(f"missing: {', '.join(missing)}")
    if problems:
        accepted = ", ".join(f"{p.name}{' (required)' if p.required else ''}" for p in info.params) or "none"
        raise NodeUserError(f"{info.endpoint} rejected the request ({'; '.join(problems)}). Accepted: {accepted}.")
    return dict(request)


# ---------------------------------------------------------------------------
# Calls + shaping
# ---------------------------------------------------------------------------


def _method(client: Any, endpoint: str) -> Any:
    target = client
    for part in endpoint.split("."):
        target = getattr(target, part)
    return target


async def invoke(client: Any, endpoint: str, request: Mapping[str, Any], *, writes: bool) -> Any:
    """``client.<endpoint>(**request)`` as plain data. A list method answers
    with its first page (the request's ``page_number`` / ``page_size`` pick
    which): the SDK's own auto-pagination asks for page 2 after page 0."""
    from dodopayments import APIError

    try:
        return to_plain(await _method(client, endpoint)(**request))
    except APIError as exc:
        raise_user_error(exc, endpoint, writes=writes)
    except ValueError as exc:  # the SDK's own argument checks, e.g. an empty id
        raise NodeUserError(f"Dodo Payments {endpoint} rejected the request: {exc}") from exc


def to_plain(value: Any) -> Any:
    """The API's own JSON: a model with the fields Dodo sent, a page as
    ``{"items": [...]}``."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [to_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: to_plain(item) for key, item in value.items()}
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return to_dict(mode="json")
    raise NodeUserError(
        f"This endpoint answers with a file ({type(value).__name__}), such as an invoice PDF, "
        "which the node cannot return."
    )


def _preview(value: Any) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= _BODY_PREVIEW_CHARS else text[:_BODY_PREVIEW_CHARS] + "..."


def raise_user_error(exc: Exception, endpoint: str, *, writes: bool) -> NoReturn:
    """Translate an SDK ``APIError`` into a ``NodeUserError`` carrying Dodo's
    own error code and message (the body is ``{"code", "message"}``)."""
    from dodopayments import (
        APIConnectionError,
        APIStatusError,
        APITimeoutError,
        AuthenticationError,
        InternalServerError,
        PermissionDeniedError,
        RateLimitError,
    )

    unknown_outcome = False
    if isinstance(exc, APIStatusError):
        body = exc.body if isinstance(exc.body, dict) else {}
        code = str(body.get("code") or "")
        detail = str(body.get("message") or "") or _preview(exc.body)
        message = f"Dodo Payments {endpoint} failed ({exc.status_code}{' ' + code if code else ''}): {detail}"
        if isinstance(exc, AuthenticationError):
            message += ". The API key was rejected: validate it again in Credentials -> Dodo Payments."
        elif isinstance(exc, PermissionDeniedError):
            message += ". The key lacks this permission (a read-only key cannot write), or the business is not live yet."
        elif isinstance(exc, RateLimitError):
            message += ". Dodo Payments rate-limited the request; try again shortly."
        elif isinstance(exc, InternalServerError):
            unknown_outcome = True
    elif isinstance(exc, APITimeoutError):
        message = f"Dodo Payments {endpoint} timed out."
        unknown_outcome = True
    elif isinstance(exc, APIConnectionError):
        message = f"Could not reach Dodo Payments for {endpoint}: {exc}"
        unknown_outcome = True
    else:
        message = f"Dodo Payments {endpoint} failed: {exc}"
    if writes and unknown_outcome:
        message += " The change may or may not have been made: look it up before trying again."
    raise NodeUserError(message) from exc


# ---------------------------------------------------------------------------
# Dropdown loader
# ---------------------------------------------------------------------------


async def load_dodo_payments_endpoints(params: Dict[str, Any]) -> List[Dict[str, Any]]:
    """``loadOptionsMethod`` for the ``endpoint`` dropdown. A missing SDK must
    never break the parameter panel, so it degrades to no options."""
    try:
        index = await asyncio.to_thread(endpoint_index)
    except NodeUserError as exc:
        logger.warning("dodo payments endpoint loader failed", error=str(exc))
        return []
    return [
        {"value": entry.endpoint, "label": entry.endpoint, "description": f"{entry.http_method} {entry.path}"}
        for entry in index
    ]


__all__ = [
    "CALL_TIMEOUT_SECONDS",
    "EndpointInfo",
    "ParamInfo",
    "READ_RETRIES",
    "check_request",
    "endpoint_index",
    "endpoint_writes",
    "invoke",
    "key_mode",
    "load_dodo_payments_endpoints",
    "open_client",
    "raise_user_error",
    "resolve_endpoint",
    "to_plain",
]
