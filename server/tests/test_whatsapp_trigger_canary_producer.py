"""Wave 12 C1 rollout #4: whatsappReceive producer invariant.

Locks the contract: ``nodes.whatsapp._events.emit_whatsapp_message_received``
sends incoming messages through the Temporal-durable
``services.events.dispatch.emit`` path so canary listeners can consume
them, without broadcasting them to every socket. Outbound messages are
not emitted or broadcast at all: nothing consumes them.

Same regex-introspection + runtime smoke pattern used for
webhookTrigger / chatTrigger / taskTrigger / telegramReceive
producers in this series.
"""

from __future__ import annotations

import inspect
import sys
import types
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest


if "cli" not in sys.modules:
    _cli_stub = types.ModuleType("cli")
    _cli_stub.__path__ = []
    sys.modules["cli"] = _cli_stub
    _opencompany_tcp = types.ModuleType("cli.tcp")
    _opencompany_tcp.probe_tcp_port = MagicMock(return_value=False)
    sys.modules["cli.tcp"] = _opencompany_tcp


class TestWhatsappProducerDualEmit:
    """Producer wrapper sends received messages to the canary path."""

    def test_redelivery_keeps_the_id_scoped_to_chat_and_direction(self):
        from nodes.whatsapp._events import whatsapp_message_event

        payload = {"chat_id": "123@s.whatsapp.net", "message_id": "abc", "text": "hello"}
        original = whatsapp_message_event("received", payload)
        redelivery = whatsapp_message_event("received", {**payload, "timestamp": "later"})
        another_chat = whatsapp_message_event("received", {**payload, "chat_id": "456@s.whatsapp.net"})
        another_message = whatsapp_message_event("received", {**payload, "message_id": "def"})
        sent = whatsapp_message_event("sent", payload)

        assert original.id == redelivery.id
        assert len({original.id, another_chat.id, another_message.id, sent.id}) == 4

    @pytest.mark.parametrize("payload", [{"chat_id": "123@s.whatsapp.net"}, {"message_id": "abc"}, {}])
    def test_missing_provider_identity_does_not_deduplicate_unrelated_messages(self, payload):
        from nodes.whatsapp._events import whatsapp_message_event

        assert whatsapp_message_event("received", payload).id != whatsapp_message_event("received", payload).id

    def test_emitter_is_async(self):
        from nodes.whatsapp._events import emit_whatsapp_message_received

        assert inspect.iscoroutinefunction(emit_whatsapp_message_received)

    @pytest.mark.asyncio
    async def test_received_message_reaches_triggers_and_is_not_broadcast(self, monkeypatch):
        """The message is a customer's content: it goes to the deployed
        triggers only, never to every connected socket (RFC-0005 Phase 0.9)."""
        from nodes.whatsapp import _events
        from services.events import dispatch as dispatch_mod
        from services import status_broadcaster as sb

        emit_calls: List[Dict[str, Any]] = []

        async def fake_emit(event, **kwargs):
            emit_calls.append({"event": event, **kwargs})
            return event

        broadcaster = MagicMock()
        broadcaster.broadcast = AsyncMock()

        monkeypatch.setattr(sb, "get_status_broadcaster", lambda: broadcaster)
        monkeypatch.setattr(dispatch_mod, "emit", fake_emit)

        await _events.emit_whatsapp_message_received(
            {
                "chat_id": "123@s.whatsapp.net",
                "text": "hello",
                "message_id": "abc",
                "from": "+1234567890",
            },
        )

        broadcaster.broadcast.assert_not_awaited()
        assert len(emit_calls) == 1
        envelope = emit_calls[0]["event"]
        assert envelope.type == "com.opencompany.whatsapp.message.received"
        assert envelope.subject == "123@s.whatsapp.net"
        assert emit_calls[0]["wire_routing_key"] == "whatsapp_message_received"
        assert emit_calls[0]["broadcast"] is False

    def test_outbound_messages_are_not_broadcast(self):
        """The bridge's message_sent events used to carry each outbound
        message to every socket, and no client read them."""
        from nodes.whatsapp import _events, _service

        assert not hasattr(_events, "broadcast_whatsapp_message")
        assert "whatsapp_message_sent" not in inspect.getsource(_events)
        assert 'method == "event.message_sent"' not in inspect.getsource(_service)


class TestWhatsappPluginSelfRegistersCanary:
    """Importing the whatsapp plugin opts whatsappReceive into the canary."""

    def test_plugin_import_registers_whatsapp_receive(self):
        from services.deployment import canary_registry

        try:
            __import__("nodes.whatsapp")
        except ImportError as exc:  # pragma: no cover
            pytest.xfail(f"nodes.whatsapp not importable: {exc}")

        assert canary_registry.is_canary_trigger_type("whatsappReceive"), (
            "Importing nodes.whatsapp should call "
            "register_canary_trigger_type('whatsappReceive') — see "
            "the __init__.py bottom section."
        )
