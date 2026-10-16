"""services/gateway/routing.py: own keys first; the gateway serves only the
providers llm_defaults.json marks, and only when enabled and connected."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.gateway.routing import GATEWAY_CREDENTIAL_ID, GatewayRoute, GatewayRouter, gateway_covers, key_or_route

DEFAULTS = {
    "providers": {
        "openai": {"gateway": {"model_prefix": "openai/", "base_path": "/v1"}},
        "anthropic": {"gateway": {"model_prefix": "", "base_path": ""}},
        "deepseek": {"default_model": "deepseek-v4.1-flash"},
    }
}


def _router(*, enabled=True, distributed=False, key="sk-gateway", defaults=DEFAULTS):
    settings = SimpleNamespace(gateway_enabled=enabled, distributed_mode=distributed, gateway_url="https://gw.example/")
    auth = MagicMock()
    auth.get_api_key = AsyncMock(return_value=key)
    return GatewayRouter(settings=settings, auth_service=auth, defaults=defaults), auth


async def test_a_served_provider_gets_the_gateway_route():
    router, auth = _router()
    assert await router.route("openai") == GatewayRoute(
        api_key="sk-gateway", base_url="https://gw.example/v1", model_prefix="openai/"
    )
    auth.get_api_key.assert_awaited_with(GATEWAY_CREDENTIAL_ID)


async def test_native_routes_use_the_gateway_root_and_bare_ids():
    router, _ = _router()
    route = await router.route("anthropic")
    assert route is not None
    assert (route.base_url, route.model_prefix) == ("https://gw.example", "")


@pytest.mark.parametrize("provider", ["deepseek", "openai_compatible:lab", "unknown"])
async def test_a_provider_without_a_gateway_block_is_never_routed(provider):
    router, _ = _router()
    assert await router.route(provider) is None


async def test_no_route_without_a_stored_gateway_key():
    router, _ = _router(key=None)
    assert await router.route("openai") is None
    assert await router.covers("openai") is False


@pytest.mark.parametrize("flag", [False, MagicMock(), "true", 1])
async def test_only_a_true_flag_turns_the_gateway_on(flag):
    router, auth = _router(enabled=flag)
    assert await router.route("openai") is None
    auth.get_api_key.assert_not_awaited()


async def test_distributed_mode_turns_the_gateway_off():
    router, _ = _router(distributed=True)
    assert await router.route("openai") is None


async def test_openai_format_routes_put_native_providers_on_v1():
    """For the RLM library, which speaks only OpenAI's format to a custom URL."""
    defaults = {"providers": {**DEFAULTS["providers"], "anthropic": {"gateway": {
        "model_prefix": "", "base_path": "", "openai_prefix": "anthropic/"}}}}
    router, _ = _router(defaults=defaults)
    assert await router.route("anthropic", openai_format=True) == GatewayRoute(
        api_key="sk-gateway", base_url="https://gw.example/v1", model_prefix="anthropic/"
    )
    assert await router.route("openai", openai_format=True) == await router.route("openai")


async def test_every_served_provider_has_an_openai_format_route():
    from services.llm.config import LLM_DEFAULTS

    router, _ = _router(defaults=LLM_DEFAULTS)
    for provider in router.served_providers():
        route = await router.route(provider, openai_format=True)
        assert route.base_url.endswith("/v1") and route.model_prefix.endswith("/"), provider


def test_served_providers_come_from_the_gateway_blocks():
    router, _ = _router()
    assert router.served_providers() == ["openai", "anthropic"]


def test_llm_defaults_serves_exactly_the_priced_providers():
    """OpenRouter, DeepSeek and Sarvam stay off until their default models
    are priced upstream (gateway/tests/test_pins.py checks the prices)."""
    from services.llm.config import LLM_DEFAULTS

    router, _ = _router(defaults=LLM_DEFAULTS)
    assert set(router.served_providers()) == {
        "openai", "anthropic", "gemini", "groq", "cerebras", "xai", "kimi", "mistral",
    }


async def test_gateway_covers_trusts_only_a_real_router(monkeypatch):
    # Under test the container can be a MagicMock (tests/conftest.py), whose
    # router would claim to cover every provider.
    from core.container import container

    monkeypatch.setattr(container, "gateway_router", MagicMock(), raising=False)
    assert await gateway_covers("openai") is False


async def test_the_gateway_default_is_gemini_while_the_gateway_serves_it(monkeypatch):
    from services.gateway import routing
    from services.llm.config import LLM_DEFAULTS

    covered = {"gemini"}

    async def covers(provider):
        return provider in covered

    monkeypatch.setattr(routing, "gateway_covers", covers)
    assert LLM_DEFAULTS["gateway"]["default_provider"] == "gemini"
    assert await routing.gateway_default() == ("gemini", LLM_DEFAULTS["providers"]["gemini"]["default_model"])
    covered.clear()
    assert await routing.gateway_default() is None


ROUTE = GatewayRoute(api_key="sk-gateway", base_url="https://gw.example/v1", model_prefix="openai/")


def _node_ctx(*, key=None, reason="missing"):
    """A NodeContext stand-in whose connection yields ``key`` or raises the
    annotated PermissionError ApiKeyCredential.resolve raises."""

    async def credentials():
        if key:
            return {"api_key": key}
        error = PermissionError("No API key for 'openai'.")
        error.reason = reason
        raise error

    @asynccontextmanager
    async def connection(credential_id):
        yield SimpleNamespace(credentials=credentials)

    return SimpleNamespace(connection=connection)


async def test_a_node_uses_its_own_key_without_asking_the_gateway(monkeypatch):
    route = AsyncMock(return_value=ROUTE)
    monkeypatch.setattr("services.gateway.routing.gateway_route", route)
    assert await key_or_route(_node_ctx(key="sk-own"), "openai", "openai") == ("sk-own", None)
    route.assert_not_awaited()


async def test_a_node_without_its_own_key_gets_the_gateway_key_and_route(monkeypatch):
    monkeypatch.setattr("services.gateway.routing.gateway_route", AsyncMock(return_value=ROUTE))
    assert await key_or_route(_node_ctx(), "openai", "openai") == ("sk-gateway", ROUTE)


async def test_a_node_without_any_key_still_raises_the_missing_key_error(monkeypatch):
    monkeypatch.setattr("services.gateway.routing.gateway_route", AsyncMock(return_value=None))
    with pytest.raises(PermissionError) as raised:
        await key_or_route(_node_ctx(), "openai", "openai")
    assert raised.value.reason == "missing"


async def test_only_a_missing_key_falls_back_to_the_gateway(monkeypatch):
    route = AsyncMock(return_value=ROUTE)
    monkeypatch.setattr("services.gateway.routing.gateway_route", route)
    with pytest.raises(PermissionError):
        await key_or_route(_node_ctx(reason="expired"), "openai", "openai")
    route.assert_not_awaited()


async def test_activity_key_lookup_returns_no_key_when_the_gateway_covers(monkeypatch):
    """The gateway key never reaches activity code or a Temporal payload:
    the activity passes no key and ChatUnifier routes the call."""
    from core.container import container
    from services.temporal import agent_activities

    auth = MagicMock(distributed_credentials=False)
    auth.resolve_api_key = AsyncMock(return_value=None)
    monkeypatch.setattr(container, "auth_service", MagicMock(return_value=auth))
    monkeypatch.setattr("services.gateway.routing.gateway_covers", AsyncMock(return_value=True))

    assert await agent_activities._resolve_activity_api_key({"provider": "openai", "user_id": "owner"}) == ""


async def test_activity_key_lookup_still_refuses_when_nothing_covers(monkeypatch):
    from temporalio.exceptions import ApplicationError

    from core.container import container
    from services.temporal import agent_activities

    auth = MagicMock(distributed_credentials=False)
    auth.resolve_api_key = AsyncMock(return_value=None)
    monkeypatch.setattr(container, "auth_service", MagicMock(return_value=auth))
    monkeypatch.setattr("services.gateway.routing.gateway_covers", AsyncMock(return_value=False))

    with pytest.raises(ApplicationError) as raised:
        await agent_activities._resolve_activity_api_key({"provider": "openai", "user_id": "owner"})
    assert raised.value.type == "MissingAgentProviderCredential"
