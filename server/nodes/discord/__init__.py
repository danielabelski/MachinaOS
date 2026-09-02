"""Discord plugin: bot messaging, REST actions and inbound events.

Pure wiring. Importing the node modules is what registers them --
``BaseNode.__init_subclass__`` does that work.

``import discord`` never appears at module scope anywhere in this package.
``nodes/__init__.py`` swallows import errors during discovery, so a library
import that failed here would make the whole plugin silently disappear rather
than report anything. The gateway imports it inside a function.
"""

from __future__ import annotations

from services.deployment.canary_registry import register_canary_trigger_type
from services.event_waiter import register_filter_builder, register_trigger_precheck
from services.node_output_schemas import register_output_schema
from services.plugin.shutdown_hooks import register_shutdown_hook
from services.plugin.social_provider_registry import register_social_send_handler
from services.status_broadcaster import register_service_refresh
from services.ws_handler_registry import (
    register_oauth_callback_path,
    register_option_loader,
    register_router,
    register_ws_handlers,
)

from ._credentials import DiscordBotCredential
from ._events import INTERACTION_CREATED_TYPE, MESSAGE_RECEIVED_TYPE
from ._filters import build_discord_filter, build_interaction_filter
from ._gateway import stop_all_gateways
from ._handlers import WS_HANDLERS
from ._option_loaders import load_accounts, load_channels, load_guilds
from ._refresh import precheck_discord_trigger, refresh_discord_status
from ._router import router as discord_router
from ._social import social_send_adapter
from .discord_action import DiscordActionNode, DiscordActionOutput
from .discord_interaction import DiscordInteractionNode, DiscordInteractionOutput
from .discord_receive import DiscordReceiveNode, DiscordReceiveOutput
from .discord_send import DiscordSendNode, DiscordSendOutput

register_ws_handlers(WS_HANDLERS)

# POST /api/discord/interactions[/{account_id}] and GET /api/discord/callback.
register_router(discord_router, name="discord")
register_oauth_callback_path("discord", "/api/discord/callback")

register_option_loader("discordAccounts", load_accounts)
register_option_loader("discordGuilds", load_guilds)
register_option_loader("discordChannels", load_channels)

register_output_schema(DiscordSendNode.type, DiscordSendOutput)
register_output_schema(DiscordActionNode.type, DiscordActionOutput)
register_output_schema(DiscordReceiveNode.type, DiscordReceiveOutput)
register_output_schema(DiscordInteractionNode.type, DiscordInteractionOutput)

register_filter_builder(DiscordReceiveNode.type, build_discord_filter)
register_filter_builder(DiscordInteractionNode.type, build_interaction_filter)
register_trigger_precheck(DiscordReceiveNode.type, precheck_discord_trigger)
register_service_refresh(refresh_discord_status)

# The second argument becomes the EventType Search Attribute the Temporal
# Visibility query matches on, so it must equal the type the envelope in
# _events.py carries. A mismatch is silent: the listener starts and never
# receives a signal.
#
# Two registrations because the registry maps one node type to one event
# type; a single trigger could not subscribe to both.
register_canary_trigger_type(DiscordReceiveNode.type, MESSAGE_RECEIVED_TYPE)
register_canary_trigger_type(DiscordInteractionNode.type, INTERACTION_CREATED_TYPE)

# socialSend offers "discord" in its channel enum; this is what makes that
# entry dispatch somewhere instead of failing with "no registered handler".
register_social_send_handler("discord", social_send_adapter)

# Sessions left open count against the account's concurrent-session limit
# until Discord times them out, which across dev restarts looks like a second
# instance that will not go away.
register_shutdown_hook("discord_gateway", stop_all_gateways)

__all__ = [
    "DiscordActionNode",
    "DiscordBotCredential",
    "DiscordInteractionNode",
    "DiscordReceiveNode",
    "DiscordSendNode",
    "WS_HANDLERS",
]
