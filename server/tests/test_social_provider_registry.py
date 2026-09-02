"""Wave 12 C4 sub-piece A: social-provider registry contract + invariant.

Locks two layers:

1. **Registry surface**: idempotent registration, retrieval, snapshot.

2. **Architectural invariant**: ``nodes/social/_base.py`` does NOT
   import ``handle_whatsapp_send`` (or any other plugin's send
   function) directly. The dispatch must route through
   :func:`services.plugin.social_provider_registry.get_social_send_handler`.
   This is the regression catch for the load-bearing cross-plugin reach
   the migration closed.

Same style as ``test_canary_registry.py`` and
``test_plugin_self_containment.py`` — source-introspection-driven,
no live Temporal cluster needed.
"""

from __future__ import annotations

import inspect
import re
import sys
import types
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest


if "cli" not in sys.modules:
    _cli_stub = types.ModuleType("cli")
    _cli_stub.__path__ = []
    sys.modules["cli"] = _cli_stub
    _opencompany_tcp = types.ModuleType("cli.tcp")
    _opencompany_tcp.probe_tcp_port = MagicMock(return_value=False)
    sys.modules["cli.tcp"] = _opencompany_tcp


@pytest.fixture
def fresh_registry(monkeypatch):
    """Reset the registry's backing dict so each test runs in isolation.

    The production registry accumulates as plugins import — that's
    correct for runtime. Scope assertions must isolate from accumulated
    state so a future plugin opt-in doesn't silently flip outcomes.
    """
    from services.plugin import social_provider_registry as reg

    # IdempotentRegistry exposes its backing dict via .items() but the
    # field is `_items`. monkeypatching the inner dict is the standard
    # pattern used in other registry tests.
    fresh = type(reg._REGISTRY)(reg._REGISTRY._name)  # type: ignore[attr-defined]
    monkeypatch.setattr(reg, "_REGISTRY", fresh)
    return reg


class TestRegistryContract:
    """Surface API: register, query, snapshot."""

    def test_unregistered_platform_returns_none(self, fresh_registry):
        assert fresh_registry.get_social_send_handler("whatsapp") is None

    @pytest.mark.asyncio
    async def test_register_then_get_returns_handler(self, fresh_registry):
        captured = []
        sentinel_ctx = object()

        async def fake_handler(payload: Dict[str, Any], ctx: Any):
            captured.append((payload, ctx))
            return {"sent": True}

        fresh_registry.register_social_send_handler("whatsapp", fake_handler)
        handler = fresh_registry.get_social_send_handler("whatsapp")

        assert handler is fake_handler
        result = await handler({"recipient": "+1234567890"}, sentinel_ctx)
        assert result == {"sent": True}
        # ctx is forwarded verbatim: handlers resolve credentials through it.
        assert captured == [({"recipient": "+1234567890"}, sentinel_ctx)]

    def test_idempotent_register_same_callable(self, fresh_registry):
        async def h(payload, ctx):
            return {}

        fresh_registry.register_social_send_handler("whatsapp", h)
        fresh_registry.register_social_send_handler("whatsapp", h)
        assert fresh_registry.registered_platforms() == frozenset({"whatsapp"})

    def test_conflicting_register_raises(self, fresh_registry):
        async def h1(payload, ctx):
            return {}

        async def h2(payload, ctx):
            return {}

        fresh_registry.register_social_send_handler("whatsapp", h1)
        with pytest.raises(ValueError, match="already registered"):
            fresh_registry.register_social_send_handler("whatsapp", h2)

    def test_multiple_platforms_coexist(self, fresh_registry):
        async def h(payload, ctx):
            return {}

        for p in ("whatsapp", "telegram", "slack"):
            fresh_registry.register_social_send_handler(p, h)

        assert fresh_registry.registered_platforms() == frozenset(
            {
                "whatsapp",
                "telegram",
                "slack",
            }
        )


class TestNoCrossPluginReachInSocialBase:
    """Architectural invariant: ``nodes/social/_base.py`` must NOT
    import any plugin's ``_service`` directly. Dispatch goes through
    ``get_social_send_handler`` from the registry.

    Regression catch: if someone re-introduces the ``from nodes.whatsapp.
    _service import handle_whatsapp_send`` (or any sibling), this test
    fails at import-time with a pointer to the registry pattern.
    """

    _FORBIDDEN_PATTERN = re.compile(
        r"^\s*from\s+nodes\.\w+\._service\s+import",
        re.MULTILINE,
    )

    def test_social_base_does_not_cross_import_service(self):
        from nodes.social import _base as social_base

        src = inspect.getsource(social_base)
        match = self._FORBIDDEN_PATTERN.search(src)
        assert match is None, (
            f"nodes/social/_base.py contains a cross-plugin _service "
            f"import (matched at offset {match.start()}):\n  "
            f"{src[match.start():match.end()].strip()}\n"
            "Route through services.plugin.social_provider_registry."
            "get_social_send_handler('<platform>') instead. Each platform "
            "plugin self-registers from its __init__.py."
        )

    def test_social_base_calls_registry_lookup(self):
        """The dispatcher must query the registry — not just hide
        the cross-plugin import behind a runtime import."""
        from nodes.social import _base as social_base

        src = inspect.getsource(social_base.handle_social_send)
        assert "get_social_send_handler" in src, (
            "handle_social_send must call get_social_send_handler "
            "from services.plugin.social_provider_registry. Hardcoded "
            "fallback imports defeat the plugin-self-registration pattern."
        )

    def test_dispatcher_names_no_platform(self):
        """The send dispatcher must not branch on a platform name.

        It used to read ``if channel == "whatsapp"`` and stub everything
        else, so socialSend's channel enum advertised platforms that could
        never work. A platform name reappearing here means that branch is
        growing back.
        """
        from nodes.social import _base as social_base

        src = inspect.getsource(social_base.handle_social_send).lower()
        offenders = [p for p in ("whatsapp", "telegram", "discord", "slack") if p in src]
        assert not offenders, (
            f"handle_social_send mentions {offenders}. Platform-specific "
            "parameter mapping belongs in that plugin's registered adapter "
            "(see nodes/whatsapp/_social.py), not in the social node."
        )


# Every plugin that ships a social_send_adapter, with the channel it registers
# under. A plugin that imports its adapter but never calls
# register_social_send_handler passes every other test in this file and fails
# only at runtime, so each one is listed here explicitly.
SOCIAL_PLATFORM_PLUGINS = [
    ("nodes.whatsapp", "whatsapp"),
    ("nodes.whatsapp_business", "whatsapp_business"),
    ("nodes.discord", "discord"),
]


class TestWhatsappPluginSelfRegistersAsSocialProvider:
    """Importing a platform plugin registers its social send handler.

    The two WhatsApp plugins are separate platforms: ``nodes/whatsapp/``
    drives a personal account through the Go bridge, ``nodes/whatsapp_business/``
    the official Cloud API. They share no credential and no API, so they
    register under distinct keys and socialSend offers both. Discord is the
    third registered platform.
    """

    @pytest.mark.parametrize("module, platform", SOCIAL_PLATFORM_PLUGINS)
    def test_plugin_import_populates_registry(self, module, platform):
        from services.plugin import social_provider_registry as reg

        try:
            __import__(module)
        except ImportError as exc:  # pragma: no cover
            pytest.xfail(f"{module} not importable: {exc}")

        handler = reg.get_social_send_handler(platform)
        assert handler is not None, (
            f"Importing {module} should call "
            f"register_social_send_handler('{platform}', ...). "
            "Check the __init__.py bottom section."
        )
        assert callable(handler)

    def test_both_whatsapp_platforms_are_distinct(self):
        """A shared key would make one plugin silently shadow the other —
        and, because registration raises on a conflicting callable, would
        break startup instead."""
        from services.plugin import social_provider_registry as reg

        for module, _platform in SOCIAL_PLATFORM_PLUGINS:
            try:
                __import__(module)
            except ImportError as exc:  # pragma: no cover
                pytest.xfail(f"{module} not importable: {exc}")

        handlers = [reg.get_social_send_handler(p) for _m, p in SOCIAL_PLATFORM_PLUGINS]
        assert all(h is not None for h in handlers)
        assert len({id(h) for h in handlers}) == len(handlers)

    def test_every_registered_platform_is_selectable(self):
        """A registered platform absent from socialSend's channel enum is
        unreachable; an enum entry with no handler is a promise the node
        cannot keep. This catches the first case."""
        from nodes.social.social_send import SocialSendParams
        from services.plugin import social_provider_registry as reg

        for module, _platform in SOCIAL_PLATFORM_PLUGINS:
            try:
                __import__(module)
            except ImportError as exc:  # pragma: no cover
                pytest.xfail(f"{module} not importable: {exc}")

        channels = set(SocialSendParams.model_fields["channel"].annotation.__args__)
        missing = sorted(reg.registered_platforms() - channels)
        assert not missing, (
            f"Registered social platforms {missing} are missing from "
            "SocialSendParams.channel, so socialSend can never dispatch to them."
        )
