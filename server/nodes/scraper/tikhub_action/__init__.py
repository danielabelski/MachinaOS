"""TikHub — the ``tikhubAction`` node, an SDK-backed social scraper "flattened like a CLI".

TikHub (https://api.tikhub.io) is a pay-per-request REST API over ~1000
scraping endpoints for TikTok, Douyin, Instagram, YouTube, Twitter/X,
Xiaohongshu, Bilibili, Kuaishou, Weibo, Reddit, Threads, LinkedIn and more.
Rather than one node per platform, this node exposes the SDK's own
``resource.method`` ids: ``list_endpoints`` discovers them (no network),
``call`` invokes one with keyword arguments, ``fetch_url`` is the hybrid
share-URL parser, and ``account`` reports balance and daily usage.

This module must not import ``tikhub`` at import time — every SDK touch
lives in :mod:`._sdk`, lazily, so the plugin registers and the credentials
modal validates even when the SDK is absent.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.logging import get_logger
from services.node_output_schemas import register_output_schema
from services.plugin import ActionNode, NodeContext, NodeUserError, Operation, TaskQueue, coerce_blank_params
from services.ws_handler_registry import register_option_loader

from .._credentials import TikHubCredential
from ._sdk import (
    PLATFORMS,
    call_endpoint,
    list_endpoints as sdk_list_endpoints,
    load_tikhub_endpoints,
    make_client,
    resolve_endpoint,
    track_tikhub_usage,
)

logger = get_logger(__name__)

_CALL = {"displayOptions": {"show": {"operation": ["call"]}}}
_LIST = {"displayOptions": {"show": {"operation": ["list_endpoints"]}}}
_CALL_OR_LIST = {"displayOptions": {"show": {"operation": ["call", "list_endpoints"]}}}
_FETCH_URL = {"displayOptions": {"show": {"operation": ["fetch_url"]}}}

_FETCH_URL_ENDPOINT = "hybrid_parsing.video_data"
_USER_INFO_ENDPOINT = "tikhub_user.get_user_info"
_USER_USAGE_ENDPOINT = "tikhub_user.get_user_daily_usage"


class TikHubActionParams(BaseModel):
    operation: Literal["call", "list_endpoints", "fetch_url", "account"] = "call"

    platform: Literal[PLATFORMS] = Field(
        default="all",
        description="Platform filter for the endpoint dropdown and for list_endpoints.",
        json_schema_extra=_CALL_OR_LIST,
    )
    # ``str`` rather than a Literal on purpose: the LLM may pass any id the
    # SDK knows, and the dropdown is only a convenience over the same string.
    endpoint: str = Field(
        default="",
        description="Endpoint id as <resource>.<method> (a /api/v1/... path or full API URL also works).",
        json_schema_extra={
            "placeholder": "douyin_web.fetch_one_video",
            "dynamicOptions": True,
            "loadOptionsMethod": "tikhubEndpoints",
            "loadOptionsDependsOn": ["platform"],
            **_CALL,
        },
    )
    params: Dict[str, Any] = Field(
        default_factory=dict,
        description="Keyword arguments for the endpoint as a JSON object (see list_endpoints for names). Batch POST endpoints take {\"body\": [...]}.",
        json_schema_extra={"editor": "json", "rows": 6, **_CALL},
    )

    search: str = Field(
        default="",
        description="Case-insensitive substring over endpoint id, summary and route.",
        json_schema_extra={"placeholder": "one_video", **_LIST},
    )
    limit: int = Field(default=100, ge=1, le=2000, json_schema_extra=_LIST)

    url: str = Field(
        default="",
        description="Share URL of a post/video to parse (TikTok, Douyin, Instagram, ...).",
        json_schema_extra={"placeholder": "https://www.tiktok.com/@user/video/123", **_FETCH_URL},
    )
    minimal: bool = Field(
        default=False,
        description="Return the trimmed payload instead of the full platform response.",
        json_schema_extra=_FETCH_URL,
    )

    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, values: Any) -> Any:
        # The panel stores "" for cleared fields and renders ``params`` as
        # text; an LLM may also stringify the JSON object.
        return coerce_blank_params(cls, values, object_fields=("params",))


class TikHubActionOutput(BaseModel):
    operation: Optional[str] = None
    endpoint: Optional[str] = None
    path: Optional[str] = None
    url: Optional[str] = None
    data: Optional[Any] = None
    code: Optional[Any] = None
    router: Optional[str] = None
    platform: Optional[str] = None
    endpoints: Optional[List[Dict[str, Any]]] = None
    count: Optional[int] = None
    total: Optional[int] = None
    account: Optional[Any] = None
    usage: Optional[Any] = None
    cost_usd: Optional[float] = None

    model_config = ConfigDict(extra="allow")


class TikHubActionNode(ActionNode):
    type = "tikhubAction"
    display_name = "TikHub"
    subtitle = "Social Scraper API"
    group = ("scraper", "tool")
    description = (
        "Scrape TikTok, Douyin, Instagram, YouTube, Twitter/X, Xiaohongshu, Bilibili and more "
        "through TikHub's ~1000-endpoint API — discover endpoints, call any of them, parse a share URL, or check the account"
    )
    component_kind = "square"
    tool_name = "tikhub_action"
    tool_description = (
        "Scrape public social-media data through TikHub (api.tikhub.io): TikTok, Douyin, Instagram, "
        "YouTube, Twitter/X, Xiaohongshu, Bilibili, Kuaishou, Weibo, Reddit, Threads, LinkedIn, Zhihu "
        "and more (~1000 endpoints). Two-step flow: (1) operation=list_endpoints with platform (e.g. "
        "'tiktok') and an optional search term to discover endpoint ids, their HTTP route and accepted "
        "params; (2) operation=call with endpoint='<resource>.<method>' and params as a JSON object of "
        "keyword arguments. Examples: douyin_web.fetch_one_video {\"aweme_id\": \"...\"}; "
        "twitter_web.fetch_search_timeline {\"keyword\": \"...\"}; hybrid_parsing.video_data "
        "{\"url\": \"<share url>\"} (or simply operation=fetch_url with url=). Batch POST endpoints take "
        "one 'body' list param. operation=account returns the TikHub account (email, balance) and daily "
        "usage (tikhub_user resource). Results return as data (unwrapped from TikHub's envelope) plus "
        "code/router. Each successful call costs about $0.001 and every endpoint allows ~10 requests/s. "
        "Needs a TikHub API key in Credentials → TikHub."
    )
    handles = (
        {"name": "input-main", "kind": "input", "position": "left", "label": "Input", "role": "main"},
        {"name": "output-main", "kind": "output", "position": "right", "label": "Output", "role": "main"},
    )
    annotations = {"destructive": False, "readonly": True, "open_world": True}
    credentials = (TikHubCredential,)
    task_queue = TaskQueue.REST_API
    usable_as_tool = True
    # usable_as_tool auto-hides both handles unless declared; this node
    # must stay wirable on the canvas as well as callable as a tool.
    hide_input_handle = False
    hide_output_handle = False

    Params = TikHubActionParams
    Output = TikHubActionOutput

    @staticmethod
    def _shape(operation: str, **fields: Any) -> Dict[str, Any]:
        """``{"operation": ...}`` plus every kwarg that is not ``None``.

        Only ``None`` is dropped — an empty ``{}`` / ``[]`` ``data`` is a real
        answer from TikHub and must reach the panel.
        """
        shaped: Dict[str, Any] = {"operation": operation}
        shaped.update({k: v for k, v in fields.items() if v is not None})
        return shaped

    # ---- operations ------------------------------------------------------

    @Operation("call", cost={"service": "tikhub", "action": "call", "count": 1})
    async def call(self, ctx: NodeContext, params: TikHubActionParams) -> Any:
        endpoint = params.endpoint.strip()
        if not endpoint:
            raise NodeUserError(
                "endpoint is required (e.g. douyin_web.fetch_one_video). "
                "Use operation=list_endpoints to discover endpoint ids."
            )
        info = await resolve_endpoint(endpoint)
        async with make_client(ctx) as client:
            result = await call_endpoint(client, info, params.params)
        cost = await track_tikhub_usage(ctx, "call", info.path)
        return self._shape(
            "call",
            endpoint=info.endpoint,
            path=info.path,
            data=result["data"],
            code=result["code"],
            router=result["router"],
            cost_usd=cost,
        )

    @Operation("fetch_url", cost={"service": "tikhub", "action": "fetch_url", "count": 1})
    async def fetch_url(self, ctx: NodeContext, params: TikHubActionParams) -> Any:
        url = params.url.strip()
        if not url:
            raise NodeUserError("url is required (a share URL of the post or video to parse)")
        info = await resolve_endpoint(_FETCH_URL_ENDPOINT)
        kwargs: Dict[str, Any] = {"url": url}
        if params.minimal:
            kwargs["minimal"] = True
        async with make_client(ctx) as client:
            result = await call_endpoint(client, info, kwargs)
        cost = await track_tikhub_usage(ctx, "fetch_url", info.path)
        return self._shape(
            "fetch_url",
            url=url,
            endpoint=info.endpoint,
            path=info.path,
            data=result["data"],
            code=result["code"],
            router=result["router"],
            cost_usd=cost,
        )

    @Operation("list_endpoints")
    async def list_endpoints(self, ctx: NodeContext, params: TikHubActionParams) -> Any:
        # Pure introspection — no network, no billing, no tracking.
        matches, total = await sdk_list_endpoints(platform=params.platform, search=params.search, limit=params.limit)
        return self._shape(
            "list_endpoints",
            platform=params.platform,
            count=len(matches),
            total=total,
            endpoints=[m.as_dict() for m in matches],
        )

    @Operation("account", cost={"service": "tikhub", "action": "account", "count": 1})
    async def account(self, ctx: NodeContext, params: TikHubActionParams) -> Any:
        info = await resolve_endpoint(_USER_INFO_ENDPOINT)
        usage: Any = None
        async with make_client(ctx) as client:
            account = (await call_endpoint(client, info, {}))["data"]
            try:
                usage_info = await resolve_endpoint(_USER_USAGE_ENDPOINT)
                usage = (await call_endpoint(client, usage_info, {}))["data"]
            except NodeUserError as exc:
                # Balance is the answer that matters; usage is a bonus.
                logger.warning("tikhub daily usage unavailable", error=str(exc))
        cost = await track_tikhub_usage(ctx, "account", info.path)
        return self._shape("account", account=account, usage=usage, cost_usd=cost)


register_option_loader("tikhubEndpoints", load_tikhub_endpoints)
register_output_schema(TikHubActionNode.type, TikHubActionOutput)

__all__ = ["TikHubActionNode", "TikHubActionOutput", "TikHubActionParams"]
