"""Telegram bot service — moved from services/ to nodes/ as the
plugin-first home for all telegram business logic.

Public surface (consumed by `_handlers.py`, `telegram_send.py`,
`telegram_receive.py`, and the global `status_broadcaster`):

    get_telegram_service() -> TelegramService     module-level singleton

    service.connect(token=None) -> dict           idempotent connect; reads
                                                  token from TelegramCredential
                                                  when omitted
    service.disconnect() -> dict
    service.connected: bool
    service.owner_chat_id: int | None
    service.has_stored_token() -> bool
    service.set_owner(chat_id) -> None
    service.get_status() -> dict

    service.send_message / send_photo / send_document /
    service.send_location / send_contact            send helpers; the
                                                    parse-mode fallback on
                                                    `BadRequest("can't parse
                                                    entities")` is folded into
                                                    a single helper instead of
                                                    being copy-pasted four
                                                    times.

    service.get_me() / service.get_chat(id)         direct API helpers used by
                                                    the non-workflow WebSocket
                                                    handlers.

Auth lookups go through :class:`TelegramCredential.resolve` (lifted from
the per-key ``auth.get_api_key("telegram")`` / ``..._owner_chat_id``
calls scattered across the previous handler/router code).  The credential
class stays declarative — :class:`ApiKeyCredential` does the heavy lifting,
this service just consumes the resolved dict.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import time
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, Optional

from telegram import Bot, Update
from telegram.error import BadRequest, Conflict, NetworkError, RetryAfter
from telegram.ext import Application, ContextTypes, MessageHandler, filters
from telegram.helpers import escape_markdown

from services.plugin.singleton import ServiceSingleton

from ._credentials import TelegramCredential

logger = logging.getLogger(__name__)


# Telegram Bot API hard limits (https://core.telegram.org/bots/api):
#   sendMessage.text -> 1-4096 chars
#   sendPhoto/Video/Document/Audio.caption -> 0-1024 chars
# Exceeding these raises BadRequest("Message is too long" / "Message
# caption is too long"). We split text messages across multiple
# sends, and for media captions we truncate the caption to fit then
# emit the remainder as a follow-up text message so the LLM/user
# never silently loses content.
_TG_TEXT_LIMIT = 4096
_TG_CAPTION_LIMIT = 1024

# How often a recurring, self-healing polling condition may re-log. Telegram
# re-emits Conflict on every getUpdates retry (~6s) for as long as another
# consumer holds the token, which is unactionable after the first line.
_POLLING_WARN_INTERVAL_S = 60.0


def _tg_len(text: str) -> int:
    """Length as Telegram counts it: UTF-16 code units, not code points.

    The Bot API measures its 4096 / 1024 caps in UTF-16 units, so an emoji
    costs 2 and Python's ``len()`` under-counts. A 1024-code-point caption of
    emoji is rejected as "caption is too long" even though ``len()`` says it
    fits. See https://core.telegram.org/api/entities.
    """
    return len(text.encode("utf-16-le")) // 2


def _split_head(text: str, limit: int) -> tuple[str, str]:
    """Split off the first <=``limit`` chunk at the cleanest boundary.

    Returns ``(head, tail)``; ``tail`` is ``""`` when the whole text fits.
    Prefers paragraph -> line -> sentence -> space breaks found past the
    halfway mark so we don't leave tiny tail chunks, else cuts hard.
    """
    if _tg_len(text) <= limit:
        return text, ""

    # Start from a code-point slice, then shrink until it fits in UTF-16
    # units too (an all-emoji window is twice as long as it looks).
    window = text[:limit]
    while window and _tg_len(window) > limit:
        window = window[: len(window) - (_tg_len(window) - limit)]

    cut = -1
    for sep in ("\n\n", "\n", ". ", "! ", "? ", " "):
        idx = window.rfind(sep)
        if idx > limit // 2:
            cut = idx + len(sep)
            break
    if cut <= 0:
        # No clean break -- hard cut at the end of the fitted window.
        cut = len(window)
    return text[:cut].rstrip(), text[cut:].lstrip()


def _split_text(text: str, limit: int) -> list[str]:
    """Split ``text`` into chunks no longer than ``limit`` UTF-16 units,
    preferring paragraph -> line -> sentence -> space boundaries before
    falling back to a hard cut. Used for Telegram's 4096 / 1024 caps.

    Pure stdlib, no regex outside ``str`` ops -- the splitting heuristic
    should stay legible.
    """
    if _tg_len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while _tg_len(remaining) > limit:
        head, remaining = _split_head(remaining, limit)
        chunks.append(head)
    if remaining:
        chunks.append(remaining)
    return chunks


# Inbound content-type probes, in priority order.
#
# ORDER IS LOAD-BEARING: Telegram sets ``message.document`` on animation
# messages (an animation *is* a document carrying an extra ``animation``
# field), so probing ``document`` first misclassifies every GIF. Same for
# video_note vs video. Probe the most specific object first.
_CONTENT_PROBES: tuple[tuple[str, Callable[[Any], Any]], ...] = (
    ("photo", lambda m: m.photo),
    ("animation", lambda m: getattr(m, "animation", None)),
    ("video_note", lambda m: getattr(m, "video_note", None)),
    ("video", lambda m: m.video),
    ("voice", lambda m: m.voice),
    ("audio", lambda m: m.audio),
    ("sticker", lambda m: m.sticker),
    ("document", lambda m: m.document),
    ("location", lambda m: m.location),
    ("contact", lambda m: m.contact),
    ("poll", lambda m: m.poll),
)

# Media kinds — the subset of content types that carry a downloadable file.
_MEDIA_CONTENT_TYPES = frozenset(
    {"photo", "animation", "video_note", "video", "voice", "audio", "sticker", "document"}
)


def _detect_content_type(msg: Any) -> str:
    for name, probe in _CONTENT_PROBES:
        try:
            if probe(msg):
                return name
        except AttributeError:
            continue
    return "text"


# Per-type detail extraction. Deliberately scalar-only: no thumbnails, no
# PhotoSize arrays, no vcard blobs. Node results are amplified ~5x (Temporal
# payload -> WS broadcast -> 3 DB keys -> LLM conversation, replayed every
# turn), so each dict stays 5-10 scalars, a few hundred bytes.
_DETAIL_EXTRACTORS: Dict[str, Callable[[Any], Dict[str, Any]]] = {
    "photo": lambda m: {
        "file_id": m.photo[-1].file_id,
        "file_unique_id": m.photo[-1].file_unique_id,
        "width": m.photo[-1].width,
        "height": m.photo[-1].height,
        "file_size": m.photo[-1].file_size,
    },
    "video": lambda m: {
        "file_id": m.video.file_id,
        "file_unique_id": m.video.file_unique_id,
        "width": m.video.width,
        "height": m.video.height,
        "duration": m.video.duration,
        "file_name": m.video.file_name,
        "mime_type": m.video.mime_type,
        "file_size": m.video.file_size,
    },
    "animation": lambda m: {
        "file_id": m.animation.file_id,
        "file_unique_id": m.animation.file_unique_id,
        "width": m.animation.width,
        "height": m.animation.height,
        "duration": m.animation.duration,
        "file_name": m.animation.file_name,
        "mime_type": m.animation.mime_type,
        "file_size": m.animation.file_size,
    },
    "video_note": lambda m: {
        "file_id": m.video_note.file_id,
        "file_unique_id": m.video_note.file_unique_id,
        "length": m.video_note.length,
        "duration": m.video_note.duration,
        "file_size": m.video_note.file_size,
    },
    "audio": lambda m: {
        "file_id": m.audio.file_id,
        "file_unique_id": m.audio.file_unique_id,
        "duration": m.audio.duration,
        "performer": m.audio.performer,
        "title": m.audio.title,
        "file_name": m.audio.file_name,
        "mime_type": m.audio.mime_type,
        "file_size": m.audio.file_size,
    },
    "voice": lambda m: {
        "file_id": m.voice.file_id,
        "file_unique_id": m.voice.file_unique_id,
        "duration": m.voice.duration,
        "mime_type": m.voice.mime_type,
        "file_size": m.voice.file_size,
    },
    "sticker": lambda m: {
        "file_id": m.sticker.file_id,
        "file_unique_id": m.sticker.file_unique_id,
        "width": m.sticker.width,
        "height": m.sticker.height,
        "emoji": m.sticker.emoji,
        "set_name": m.sticker.set_name,
        "is_animated": m.sticker.is_animated,
        "is_video": m.sticker.is_video,
        "file_size": m.sticker.file_size,
    },
    "document": lambda m: {
        "file_id": m.document.file_id,
        "file_unique_id": m.document.file_unique_id,
        "file_name": m.document.file_name,
        "mime_type": m.document.mime_type,
        "file_size": m.document.file_size,
    },
    "location": lambda m: {
        "latitude": m.location.latitude,
        "longitude": m.location.longitude,
        "horizontal_accuracy": getattr(m.location, "horizontal_accuracy", None),
        "live_period": getattr(m.location, "live_period", None),
    },
    "contact": lambda m: {
        "phone_number": m.contact.phone_number,
        "first_name": m.contact.first_name,
        "last_name": m.contact.last_name,
        "user_id": m.contact.user_id,
    },
    "poll": lambda m: {
        "id": m.poll.id,
        "question": m.poll.question,
        "options": [o.text for o in (m.poll.options or [])[:12]],
        "type": m.poll.type,
        "is_anonymous": m.poll.is_anonymous,
        "allows_multiple_answers": m.poll.allows_multiple_answers,
        "is_closed": m.poll.is_closed,
        "total_voter_count": m.poll.total_voter_count,
    },
}

# Telegram omits mime_type on several kinds; synthesise it so downstream
# nodes get a usable content type without per-kind branching.
_SYNTHETIC_MIME: Dict[str, str] = {
    "photo": "image/jpeg",
    "voice": "audio/ogg",
    "video_note": "video/mp4",
    "animation": "video/mp4",
    "sticker": "image/webp",
}
_SYNTHETIC_EXT: Dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "audio/ogg": ".ogg",
    "video/mp4": ".mp4",
    "application/x-tgsticker": ".tgs",
    "video/webm": ".webm",
}


def _normalize_media(
    content_type: str,
    detail: Optional[Dict[str, Any]],
    caption: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Collapse any media kind into one type-agnostic block.

    Downstream nodes and agents should never branch on content_type to find
    a file_id — this is the single shape they read.
    """
    if content_type not in _MEDIA_CONTENT_TYPES or not detail:
        return None

    mime = detail.get("mime_type") or _SYNTHETIC_MIME.get(content_type)
    if content_type == "sticker":
        if detail.get("is_animated"):
            mime = "application/x-tgsticker"
        elif detail.get("is_video"):
            mime = "video/webm"

    file_name = detail.get("file_name")
    if not file_name:
        ext = _SYNTHETIC_EXT.get(mime or "") or mimetypes.guess_extension(mime or "") or ".bin"
        file_name = f"{detail.get('file_unique_id') or content_type}{ext}"

    return {
        "kind": content_type,
        "file_id": detail.get("file_id"),
        "file_unique_id": detail.get("file_unique_id"),
        "mime_type": mime,
        "file_name": file_name,
        "file_size": detail.get("file_size"),
        "duration": detail.get("duration"),
        "width": detail.get("width"),
        "height": detail.get("height"),
        "caption": caption or "",
        # Populated only by an explicit download; the trigger never fetches
        # bytes and never carries base64.
        "file_path": None,
        "downloaded": False,
    }


def _summarize_reply_to(replied: Any) -> Optional[Dict[str, Any]]:
    """Bounded summary of the message being replied to.

    Without this an agent sees a bare ``reply_to_message_id`` it cannot act
    on — "transcribe this" / "summarise this" needs the quoted media.
    """
    if replied is None:
        return None
    content_type = _detect_content_type(replied)
    summary: Dict[str, Any] = {
        "message_id": replied.message_id,
        "content_type": content_type,
        "text": ((replied.text or replied.caption or "")[:500]),
    }
    extractor = _DETAIL_EXTRACTORS.get(content_type)
    if extractor is not None and content_type in _MEDIA_CONTENT_TYPES:
        try:
            detail = extractor(replied)
            summary["media"] = {
                "kind": content_type,
                "file_id": detail.get("file_id"),
                "mime_type": detail.get("mime_type") or _SYNTHETIC_MIME.get(content_type),
            }
        except Exception:  # noqa: BLE001
            pass
    return summary


class TelegramService(ServiceSingleton):
    """Singleton service for Telegram bot operations.

    Inherits :meth:`ServiceSingleton.instance` for the lazy accessor.
    Overrides :meth:`reset_instance` because telegram has a side effect
    on reset — it must drain the bot's polling task before clearing.
    """

    _lock = asyncio.Lock()

    def __init__(self):
        self._application: Optional[Application] = None
        self._bot: Optional[Bot] = None
        self._token: Optional[str] = None
        self._connected: bool = False
        self._polling_task: Optional[asyncio.Task] = None
        self._bot_info: Dict[str, Any] = {}
        self._owner_chat_id: Optional[int] = None
        # Throttle state for recurring polling conditions (see _log_throttled).
        self._polling_error_counts: Dict[str, int] = {}
        self._polling_error_last_log: Dict[str, float] = {}

    @classmethod
    async def reset_instance(cls):  # type: ignore[override]
        existing = cls.__dict__.get("_instance")
        if existing is not None:
            await existing.disconnect()
            cls._instance = None

    @property
    def connected(self) -> bool:
        return self._connected and self._application is not None

    @property
    def owner_chat_id(self) -> Optional[int]:
        """Bot owner's chat ID (auto-captured on first private message).
        Falls back to TELEGRAM_OWNER_CHAT_ID env var."""
        if self._owner_chat_id:
            return self._owner_chat_id
        env_owner = os.environ.get("TELEGRAM_OWNER_CHAT_ID")
        if env_owner:
            try:
                self._owner_chat_id = int(env_owner)
                return self._owner_chat_id
            except ValueError:
                pass
        return None

    async def set_owner(self, chat_id: int):
        self._owner_chat_id = chat_id
        logger.info(f"[Telegram] Owner chat_id restored: {chat_id}")

    def get_status(self) -> Dict[str, Any]:
        return {
            "connected": self._connected,
            "bot_id": self._bot_info.get("id"),
            "bot_username": self._bot_info.get("username"),
            "bot_name": self._bot_info.get("first_name"),
            "polling_active": self._polling_task is not None and not self._polling_task.done(),
            "owner_chat_id": self._owner_chat_id,
        }

    # =========================================================================
    # Connection lifecycle
    # =========================================================================

    async def has_stored_token(self) -> bool:
        """Whether the `telegram` bot-token credential is persisted in auth."""
        try:
            secrets = await TelegramCredential.resolve()
            return bool(secrets.get("api_key"))
        except PermissionError:
            return False

    async def connect(self, token: Optional[str] = None) -> Dict[str, Any]:
        """Connect to Telegram with bot token and start polling.

        When ``token`` is omitted, falls back to the stored ``telegram``
        bot-token credential — matches the Twitter/Google OAuth pattern
        where the frontend saves credentials first and then calls connect.

        Idempotent: if already connected, returns success without tearing
        down the working bot.  See the in-line comment block for the race
        this guards against.
        """
        # Resolve credentials via the declarative class instead of raw
        # ``auth.get_api_key("telegram")`` / ``..._owner_chat_id``
        # lookups.  Keeps the credential id in one place.
        secrets: Dict[str, Any] = {}
        if not token:
            try:
                secrets = await TelegramCredential.resolve()
                token = secrets.get("api_key")
            except PermissionError:
                pass
        if not token:
            return {"success": False, "error": "Bot token required"}

        async with self._lock:
            # Idempotent: avoid the disconnect-then-rebuild race that
            # caused intermittent ``send_message`` hangs against a closed
            # httpx transport when concurrent _refresh_telegram_status
            # tasks fired on overlapping WebSocket connects.
            if self._connected:
                logger.debug(
                    "[Telegram] connect() called while already connected to " f"@{self._bot_info.get('username')}, returning success",
                )
                return {
                    "success": True,
                    "bot": self._bot_info,
                    "message": (f"Already connected to @{self._bot_info.get('username')}"),
                }

            try:
                logger.info("[Telegram] Connecting with bot token...")

                # Build application. Bot info is populated by
                # ``application.initialize()`` below (it calls getMe once
                # and caches on the bot instance) — the previous code
                # also called ``bot.get_me()`` on a separate throwaway
                # Bot before building the Application, paying for a
                # second redundant network round-trip on every cold
                # start.  Drop that — single round-trip now.
                self._application = (
                    Application.builder()
                    .token(token)
                    # Long-polling timeouts (get_updates holds connection open ~10s)
                    .get_updates_read_timeout(30.0)
                    .get_updates_write_timeout(10.0)
                    .get_updates_connect_timeout(10.0)
                    .get_updates_pool_timeout(5.0)
                    # Regular API call timeouts (send_message, etc.)
                    .read_timeout(10.0)
                    .write_timeout(10.0)
                    .connect_timeout(10.0)
                    .build()
                )
                self._bot = self._application.bot
                self._token = token
                self._application.add_handler(MessageHandler(filters.ALL, self._on_message_received))

                await self._application.initialize()

                me = self._bot
                self._bot_info = {
                    "id": me.id,
                    "username": me.username,
                    "first_name": me.first_name,
                    "can_join_groups": me.can_join_groups,
                    "can_read_all_group_messages": me.can_read_all_group_messages,
                }
                logger.info(f"[Telegram] Bot validated: @{me.username} (ID: {me.id})")

                # Hydrate runtime owner_chat_id from stored credential
                # BEFORE polling starts so the pre-poll peek below can
                # short-circuit when we already know the owner. (Was
                # previously after polling -- race-prone with the
                # capture path in _on_message_received.)
                saved_owner = secrets.get("telegram_owner_chat_id")
                if saved_owner is None:
                    try:
                        saved_owner = (await TelegramCredential.resolve()).get("telegram_owner_chat_id")
                    except PermissionError:
                        saved_owner = None
                if saved_owner:
                    try:
                        self._owner_chat_id = int(saved_owner)
                    except (TypeError, ValueError):
                        pass

                # Pre-poll peek for historical owner capture: find the
                # owner in a DM the user sent before the bot connected,
                # before polling delivers the pending queue, so a
                # workflow fired by an earlier message in that queue can
                # already send to the owner. get_updates() with no offset
                # returns the queue without advancing it, so polling
                # still delivers every one of these messages.
                if self._owner_chat_id is None:
                    await self._capture_owner_from_pending_updates()

                self._polling_task = asyncio.create_task(self._run_polling())
                self._connected = True

                await self._broadcast_status()

                logger.info(f"[Telegram] Connected and polling started for @{me.username}")
                return {
                    "success": True,
                    "bot": self._bot_info,
                    "message": f"Connected to @{me.username}",
                }

            except Exception as e:
                logger.error(f"[Telegram] Connection failed: {e}")
                self._connected = False
                self._application = None
                self._bot = None
                return {"success": False, "error": str(e)}

    async def disconnect(self) -> Dict[str, Any]:
        async with self._lock:
            return await self._disconnect_internal()

    async def _disconnect_internal(self) -> Dict[str, Any]:
        try:
            logger.info("[Telegram] Disconnecting...")
            if self._polling_task and not self._polling_task.done():
                self._polling_task.cancel()
                try:
                    await self._polling_task
                except asyncio.CancelledError:
                    pass
            if self._application:
                try:
                    await self._application.stop()
                    await self._application.shutdown()
                except Exception as e:
                    logger.warning(f"[Telegram] Shutdown warning: {e}")

            self._application = None
            self._bot = None
            self._token = None
            self._connected = False
            self._polling_task = None
            self._owner_chat_id = None
            self._bot_info = {}

            await self._broadcast_status()
            logger.info("[Telegram] Disconnected")
            return {"success": True, "message": "Disconnected"}
        except Exception as e:
            logger.error(f"[Telegram] Disconnect error: {e}")
            return {"success": False, "error": str(e)}

    # =========================================================================
    # Polling loop
    # =========================================================================

    def _log_throttled(self, key: str, message: str) -> None:
        """Log ``message`` at WARNING at most once per interval per ``key``.

        Suppressed repeats are counted and reported on the next emission, so
        a condition that recurs on every poll retry is visible without
        flooding the console.
        """
        now = time.monotonic()
        last = self._polling_error_last_log.get(key)
        if last is not None and (now - last) < _POLLING_WARN_INTERVAL_S:
            self._polling_error_counts[key] = self._polling_error_counts.get(key, 0) + 1
            return
        suppressed = self._polling_error_counts.pop(key, 0)
        if suppressed and last is not None:
            message = f"{message} (repeated {suppressed}x in the last {int(now - last)}s)"
        self._polling_error_last_log[key] = now
        logger.warning(message)

    def _on_polling_error(self, error) -> None:
        """python-telegram-bot ``error_callback`` for the getUpdates loop.

        Everything reaching here is a *polling attempt* failure that PTB
        retries on its own — the bot stays connected and the loop keeps
        running. Only genuinely unexpected errors deserve ERROR; the two
        common transients would otherwise repeat every few seconds forever
        and read as fatal when nothing is broken.
        """
        if isinstance(error, NetworkError):
            logger.debug(f"[Telegram] Network error during polling (auto-retrying): {error}")
        elif isinstance(error, Conflict):
            # Telegram allows exactly one getUpdates consumer per token.
            # Emitted on every retry (~6s) for as long as the other consumer
            # holds the slot, so it is throttled: the first line is the
            # actionable one, the rest are noise.
            self._log_throttled(
                "conflict",
                "[Telegram] Another getUpdates consumer holds this bot token, so "
                "inbound messages are paused. Polling keeps retrying and recovers "
                "on its own once the other consumer stops. The bot stays "
                "connected and sending still works. Usual causes: a second dev "
                "server or deployed instance using the same token, or the previous "
                "run's long poll still draining (up to 30s after a restart).",
            )
        elif isinstance(error, RetryAfter):
            # Flow control, not a failure: PTB sleeps for the requested
            # window and resumes.
            logger.debug(f"[Telegram] Rate limited during polling (auto-retrying): {error}")
        else:
            logger.error(f"[Telegram] Polling error: {error}")

    async def _run_polling(self):
        try:
            logger.info("[Telegram] Starting polling loop...")
            await self._application.start()
            # Keep the updates Telegram held while the backend was down (up
            # to 24 hours): dropping them lost every message sent during a
            # restart. Each update is delivered once; the offset advances
            # as polling confirms it.
            await self._application.updater.start_polling(
                drop_pending_updates=False,
                allowed_updates=Update.ALL_TYPES,
                error_callback=self._on_polling_error,
            )
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            logger.info("[Telegram] Polling cancelled")
            raise
        except Exception as e:
            # Distinct wording from _on_polling_error on purpose: reaching
            # here means the loop itself died and the bot is now marked
            # disconnected, which the retryable per-attempt failures are not.
            logger.error(f"[Telegram] Polling loop stopped, bot disconnected: {e}")
            self._connected = False
            await self._broadcast_status()

    async def _capture_owner_from_pending_updates(self) -> None:
        """Inspect the bot's pending update queue (24h Telegram retention)
        for any private message and capture the owner if found. Called
        from connect() before polling starts, so the owner is known before
        polling delivers the queued messages.

        Uses the same atomic-write-through invariant as
        ``_on_message_received``: persist FIRST, set in-memory only on
        successful persist, so a restart can re-capture cleanly if the
        DB write fails.
        """
        try:
            # offset=None / 0 returns the queue without advancing the
            # confirmed offset. allowed_updates=["message"] skips
            # callback_query / poll / inline_query payloads we don't
            # care about for owner capture.
            pending = await self._bot.get_updates(
                timeout=0,
                allowed_updates=["message"],
            )
        except Exception as e:
            logger.warning(
                f"[Telegram] Pre-poll owner-capture peek failed "
                f"({type(e).__name__}): {e}. Owner capture will fall "
                f"back to live polling once the user DMs the bot.",
            )
            return

        for upd in pending:
            m = upd.message
            if not (m and m.chat and m.chat.type == "private" and m.from_user):
                continue
            captured_id = m.from_user.id
            try:
                from services.plugin.deps import get_auth_service

                await get_auth_service().store_api_key(
                    "telegram_owner_chat_id",
                    str(captured_id),
                    models=[],
                )
            except Exception as persist_err:
                logger.error(
                    f"[Telegram] FAILED to persist retroactively-captured "
                    f"owner {captured_id}: {persist_err}. Workaround: "
                    f"set TELEGRAM_OWNER_CHAT_ID={captured_id} in .env",
                    exc_info=True,
                )
                return
            self._owner_chat_id = captured_id
            logger.info(f"[Telegram] Owner retroactively captured from pending " f"updates: @{m.from_user.username} (ID: {captured_id})")
            return

    async def _on_message_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            if not update.message:
                return
            msg = update.message

            # Auto-capture bot owner from first private message.
            # ATOMIC WRITE-THROUGH: persist to credentials DB FIRST, set
            # in-memory only on success. The previous order
            # (in-memory then await persist, with WARNING-level logging
            # of failures) had a silent failure mode: if the persist
            # raised, the rest of the process lifetime worked correctly,
            # then the next restart wiped in-memory and DB had nothing
            # -- subsequent telegramSend(recipient_type=self) failed
            # forever with "Bot owner not detected" and no trace of
            # what went wrong. The new order preserves the invariant
            # "in-memory has owner => DB has owner" so a restart can
            # always re-capture from the next inbound private message.
            if self._owner_chat_id is None and msg.chat.type == "private" and msg.from_user:
                captured_id = msg.from_user.id
                persist_ok = False
                try:
                    from services.plugin.deps import get_auth_service

                    await get_auth_service().store_api_key(
                        "telegram_owner_chat_id",
                        str(captured_id),
                        models=[],
                    )
                    persist_ok = True
                except Exception as persist_err:
                    # ERROR-level so the failure surfaces in default
                    # uvicorn output, not just DEBUG. Includes the
                    # captured id so the user can manually set
                    # TELEGRAM_OWNER_CHAT_ID in .env as an immediate
                    # workaround. exc_info=True so the underlying
                    # cause (encryption not initialised, DB locked,
                    # disk full, ...) is visible.
                    logger.error(
                        f"[Telegram] FAILED to persist owner chat_id "
                        f"{captured_id}: {persist_err}. The bot will not "
                        f"recover this binding on restart. Workaround: "
                        f"set TELEGRAM_OWNER_CHAT_ID={captured_id} in .env",
                        exc_info=True,
                    )
                if persist_ok:
                    self._owner_chat_id = captured_id
                    logger.info(f"[Telegram] Owner detected and persisted: " f"@{msg.from_user.username} (ID: {captured_id})")
                    await self._broadcast_status()

            event_data = self._format_message(msg)
            logger.info(
                f"[Telegram] Message received: {event_data.get('content_type')} from "
                f"{event_data.get('from_username', event_data.get('from_id'))}, "
                f"chat_type={event_data.get('chat_type')}"
            )

            # Route via plugin _events.py wrapper — canary CloudEvents
            # path (Visibility-query Signal + WS broadcast).
            from . import dispatch_telegram_message_received

            await dispatch_telegram_message_received(event_data)
        except Exception as e:
            logger.error(f"[Telegram] Message handler error: {e}")

    def _format_message(self, msg) -> Dict[str, Any]:
        """Format Telegram message to unified event data."""
        content_type = _detect_content_type(msg)

        data = {
            "message_id": msg.message_id,
            "chat_id": msg.chat.id,
            "chat_type": msg.chat.type,
            "chat_title": msg.chat.title,
            "from_id": msg.from_user.id if msg.from_user else None,
            "from_username": msg.from_user.username if msg.from_user else None,
            "from_first_name": msg.from_user.first_name if msg.from_user else None,
            "from_last_name": msg.from_user.last_name if msg.from_user else None,
            "is_bot": msg.from_user.is_bot if msg.from_user else False,
            # ``text`` keeps its historical caption fallback: workflows read it
            # on photo messages. ``caption`` below is additive, never a swap.
            "text": msg.text or msg.caption or "",
            "caption": msg.caption or "",
            "content_type": content_type,
            "date": msg.date.isoformat() if msg.date else datetime.now().isoformat(),
            "reply_to_message_id": msg.reply_to_message.message_id if msg.reply_to_message else None,
            # Album correlation: several media messages sharing this id were
            # sent as one album.
            "media_group_id": getattr(msg, "media_group_id", None),
        }

        extractor = _DETAIL_EXTRACTORS.get(content_type)
        if extractor is not None:
            try:
                data[content_type] = extractor(msg)
            except Exception as exc:  # noqa: BLE001
                # A malformed/partial payload from one message must never stop
                # the event reaching its workflow.
                logger.warning(
                    "[Telegram] Could not extract %s details: %s", content_type, exc
                )

        media = _normalize_media(content_type, data.get(content_type), msg.caption)
        data["media"] = media
        data["has_media"] = media is not None

        reply_to = _summarize_reply_to(getattr(msg, "reply_to_message", None))
        if reply_to is not None:
            data["reply_to"] = reply_to
        return data

    async def _broadcast_status(self):
        try:
            # Wave 12 B3: route via plugin _events.py wrapper.
            from . import broadcast_telegram_status

            await broadcast_telegram_status(
                connected=self._connected,
                bot_id=self._bot_info.get("id"),
                bot_username=self._bot_info.get("username"),
                bot_name=self._bot_info.get("first_name"),
                owner_chat_id=self._owner_chat_id,
            )
        except Exception as e:
            logger.warning(f"[Telegram] Status broadcast failed: {e}")

    # =========================================================================
    # Send helpers — parse-mode preprocessing + BadRequest fallback in one place
    # =========================================================================

    @staticmethod
    def _escape_text(text: Optional[str], parse_mode: Optional[str]) -> Optional[str]:
        if not text or not parse_mode:
            return text
        if parse_mode == "MarkdownV2":
            return escape_markdown(text, version=2)
        if parse_mode == "Markdown":
            return escape_markdown(text, version=1)
        return text

    @staticmethod
    def _format_auto(text: Optional[str]) -> tuple[Optional[str], str]:
        if not text:
            return text, "HTML"
        from services.markdown_formatter import to_telegram_html

        return to_telegram_html(text), "HTML"

    @classmethod
    def _resolve_body(cls, body: Optional[str], parse_mode: Optional[str]) -> tuple[Optional[str], Optional[str]]:
        """Apply parse-mode preprocessing to a text/caption.  Returns
        ``(body, effective_parse_mode)``."""
        if parse_mode == "Auto":
            return cls._format_auto(body)
        return cls._escape_text(body, parse_mode), parse_mode or None

    async def _send_with_parse_fallback(
        self,
        method: Callable[..., Awaitable[Any]],
        *,
        body_kw: str,
        body: Optional[str],
        parse_mode: Optional[str],
        **kwargs: Any,
    ) -> Any:
        """Call ``method(**{body_kw: processed_body, parse_mode: pm}, **kwargs)``;
        on Telegram ``BadRequest("can't parse entities")`` retry once with the
        raw body and ``parse_mode=None``.  Folds 4 copies of the same try/except
        into a single helper.
        """
        processed, effective_pm = self._resolve_body(body, parse_mode)
        try:
            return await method(
                **{body_kw: processed},
                parse_mode=effective_pm,
                **kwargs,
            )
        except BadRequest as e:
            if "can't parse entities" in str(e).lower() and effective_pm:
                logger.warning(f"[Telegram] Parse mode {effective_pm} failed for " f"{method.__name__}, sending as plain text")
                return await method(
                    **{body_kw: body},
                    parse_mode=None,
                    **kwargs,
                )
            raise

    def _require_bot(self) -> Bot:
        if not self._bot:
            raise ValueError("Telegram bot not connected")
        return self._bot

    async def send_message(
        self,
        chat_id: str | int,
        text: str,
        parse_mode: Optional[str] = None,
        disable_notification: bool = False,
        reply_to_message_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        bot = self._require_bot()
        # Telegram caps sendMessage.text at 4096. Split into chunks and
        # emit a chain (each chunk replies to the previous one so they
        # render as a thread). Returns the FIRST message's metadata
        # plus a parts/message_ids summary so callers can track the
        # whole chain.
        chunks = _split_text(text, _TG_TEXT_LIMIT)
        if len(chunks) > 1:
            logger.info(
                "[Telegram] Splitting %d-char message into %d parts",
                len(text),
                len(chunks),
            )

        first_msg: Any = None
        message_ids: list[int] = []
        reply_to = reply_to_message_id
        for chunk in chunks:
            msg = await self._send_with_parse_fallback(
                bot.send_message,
                body_kw="text",
                body=chunk,
                parse_mode=parse_mode,
                chat_id=chat_id,
                disable_notification=disable_notification,
                reply_to_message_id=reply_to,
            )
            if first_msg is None:
                first_msg = msg
            message_ids.append(msg.message_id)
            # Thread subsequent chunks under the previous chunk.
            reply_to = msg.message_id

        return {
            "message_id": first_msg.message_id,
            "chat_id": first_msg.chat.id,
            "date": first_msg.date.isoformat(),
            "text": first_msg.text,
            "parts": len(chunks),
            "message_ids": message_ids,
        }

    async def _send_captioned_media(
        self,
        method: Callable[..., Awaitable[Any]],
        *,
        media_kw: str,
        media: Any,
        caption: Optional[str],
        parse_mode: Optional[str],
        chat_id: str | int,
        disable_notification: bool = False,
        reply_to_message_id: Optional[int] = None,
        **extras: Any,
    ) -> Dict[str, Any]:
        """Send one media message, spilling an over-long caption.

        Telegram caps captions at 1024 UTF-16 units and rejects the whole
        send with BadRequest("Message caption is too long") past that. We
        truncate at the cap and thread the remainder underneath as a reply,
        so the content is never silently lost — the behaviour the limit
        constants above have documented since they were introduced.

        Ordering matters: the caption is split while still **raw**, before
        ``_resolve_body`` converts markdown to HTML. Splitting after
        conversion could cut a ``<b>`` from its closing tag and produce
        BadRequest("can't find end of the entity"). Telegram measures the
        cap against entity-parsed text, so truncating the raw markdown is
        conservative and can never come out over-long.
        """
        head, tail = _split_head(caption, _TG_CAPTION_LIMIT) if caption else ("", "")
        if tail:
            logger.info(
                "[Telegram] Caption is %d units (cap %d); remainder threaded as a follow-up",
                _tg_len(caption or ""),
                _TG_CAPTION_LIMIT,
            )

        msg = await self._send_with_parse_fallback(
            method,
            body_kw="caption",
            body=head or None,
            parse_mode=parse_mode,
            chat_id=chat_id,
            **{media_kw: media},
            disable_notification=disable_notification,
            reply_to_message_id=reply_to_message_id,
            **extras,
        )

        follow_up_ids: list[int] = []
        if tail:
            # send_message already chunks at 4096 and chains replies, so a
            # very long remainder threads under itself correctly.
            spill = await self.send_message(
                chat_id=chat_id,
                text=tail,
                parse_mode=parse_mode,
                disable_notification=True,  # one notification per logical message
                reply_to_message_id=msg.message_id,
            )
            follow_up_ids = list(spill.get("message_ids") or [])

        return {
            "message_id": msg.message_id,
            "chat_id": msg.chat.id,
            "date": msg.date.isoformat(),
            "caption_truncated": bool(tail),
            "follow_up_message_ids": follow_up_ids,
        }

    async def send_photo(
        self,
        chat_id: str | int,
        photo: str,
        caption: Optional[str] = None,
        parse_mode: Optional[str] = None,
        disable_notification: bool = False,
        reply_to_message_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        bot = self._require_bot()
        return await self._send_captioned_media(
            bot.send_photo,
            media_kw="photo",
            media=photo,
            caption=caption,
            parse_mode=parse_mode,
            chat_id=chat_id,
            disable_notification=disable_notification,
            reply_to_message_id=reply_to_message_id,
        )

    async def send_document(
        self,
        chat_id: str | int,
        document: str,
        caption: Optional[str] = None,
        parse_mode: Optional[str] = None,
        disable_notification: bool = False,
        reply_to_message_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        bot = self._require_bot()
        return await self._send_captioned_media(
            bot.send_document,
            media_kw="document",
            media=document,
            caption=caption,
            parse_mode=parse_mode,
            chat_id=chat_id,
            disable_notification=disable_notification,
            reply_to_message_id=reply_to_message_id,
        )

    async def send_location(
        self,
        chat_id: str | int,
        latitude: float,
        longitude: float,
        disable_notification: bool = False,
        reply_to_message_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        bot = self._require_bot()
        msg = await bot.send_location(
            chat_id=chat_id,
            latitude=latitude,
            longitude=longitude,
            disable_notification=disable_notification,
            reply_to_message_id=reply_to_message_id,
        )
        return {
            "message_id": msg.message_id,
            "chat_id": msg.chat.id,
            "date": msg.date.isoformat(),
        }

    async def send_contact(
        self,
        chat_id: str | int,
        phone_number: str,
        first_name: str,
        last_name: Optional[str] = None,
        disable_notification: bool = False,
        reply_to_message_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        bot = self._require_bot()
        msg = await bot.send_contact(
            chat_id=chat_id,
            phone_number=phone_number,
            first_name=first_name,
            last_name=last_name,
            disable_notification=disable_notification,
            reply_to_message_id=reply_to_message_id,
        )
        return {
            "message_id": msg.message_id,
            "chat_id": msg.chat.id,
            "date": msg.date.isoformat(),
        }

    # =========================================================================
    # Misc API helpers (used by the credentials-modal WS handlers)
    # =========================================================================

    async def get_me(self) -> Dict[str, Any]:
        bot = self._require_bot()
        me = await bot.get_me()
        return {
            "id": me.id,
            "username": me.username,
            "first_name": me.first_name,
            "can_join_groups": me.can_join_groups,
        }

    async def get_chat(self, chat_id: str | int) -> Dict[str, Any]:
        bot = self._require_bot()
        chat = await bot.get_chat(chat_id)
        return {
            "id": chat.id,
            "type": chat.type,
            "title": chat.title,
            "username": chat.username,
            "first_name": chat.first_name,
            "last_name": chat.last_name,
            "description": chat.description,
        }


def get_telegram_service() -> TelegramService:
    """Get TelegramService singleton instance."""
    return TelegramService.instance()
