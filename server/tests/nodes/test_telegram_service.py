"""TelegramService unit tests — split/caption/format logic.

Pure service tests: a ``TelegramService`` is instantiated directly and its
``_bot`` replaced with an ``AsyncMock``. No node harness, no event waiter.
This covers the text-splitting, caption-spill and inbound-formatting logic,
none of which had coverage before.

Companion to ``test_telegram_social.py``, which drives the nodes end to end
through ``NodeTestHarness``.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nodes.telegram._service import (
    _MEDIA_CONTENT_TYPES,
    _detect_content_type,
    _split_head,
    _split_text,
    _tg_len,
    TelegramService,
    _TG_CAPTION_LIMIT,
)

pytestmark = pytest.mark.node_contract


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _message(**overrides):
    """A Bot API message with every media slot empty unless overridden."""
    base = dict(
        message_id=1,
        chat=SimpleNamespace(id=555, type="private", title=None),
        from_user=SimpleNamespace(
            id=77, username="user", first_name="First", last_name=None, is_bot=False
        ),
        date=datetime(2026, 7, 25, 12, 0, 0),
        text=None,
        caption=None,
        media_group_id=None,
        reply_to_message=None,
        photo=None,
        video=None,
        audio=None,
        voice=None,
        document=None,
        sticker=None,
        animation=None,
        video_note=None,
        location=None,
        contact=None,
        poll=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _sent(message_id: int = 100):
    msg = MagicMock()
    msg.message_id = message_id
    msg.chat.id = 555
    msg.date.isoformat.return_value = "2026-07-25T12:00:00"
    msg.text = "sent"
    return msg


def _service_with_bot() -> tuple[TelegramService, MagicMock]:
    service = TelegramService()
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=lambda **kw: _sent(200))
    bot.send_photo = AsyncMock(side_effect=lambda **kw: _sent(100))
    bot.send_document = AsyncMock(side_effect=lambda **kw: _sent(101))
    service._bot = bot
    return service, bot


# ---------------------------------------------------------------------------
# length accounting + splitting
# ---------------------------------------------------------------------------


def test_tg_len_counts_utf16_units_not_code_points():
    # Telegram measures its caps in UTF-16 units, so an emoji costs 2.
    # len() says 1, which is why over-long messages used to slip through
    # and get rejected by the API.
    assert _tg_len("a") == 1
    assert _tg_len("\U0001f44d") == 2
    assert _tg_len("ab\U0001f44d") == 4


def test_split_text_returns_short_text_untouched():
    # The fits-in-one path must not rstrip or otherwise rewrite the text.
    assert _split_text("  padded  ", 100) == ["  padded  "]


def test_split_head_prefers_paragraph_then_line_then_sentence():
    head, tail = _split_head("para one.\n\npara two is longer", 15)
    assert head == "para one."
    assert tail == "para two is longer"

    head, tail = _split_head("line one\nline two is longer", 14)
    assert head == "line one"

    head, tail = _split_head("Sentence one. Sentence two is longer", 20)
    assert head == "Sentence one."


def test_split_head_hard_cuts_when_no_boundary_exists():
    head, tail = _split_head("x" * 50, 10)
    assert len(head) == 10
    assert len(tail) == 40


def test_split_text_splits_emoji_by_utf16_budget():
    # 600 emoji = 1200 UTF-16 units, so it must split at a 1024 cap even
    # though len() reports only 600 characters.
    text = "\U0001f44d" * 600
    chunks = _split_text(text, 1024)
    assert len(chunks) > 1
    assert all(_tg_len(chunk) <= 1024 for chunk in chunks)


# ---------------------------------------------------------------------------
# caption spill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_caption_under_the_limit_sends_no_follow_up():
    service, bot = _service_with_bot()
    result = await service.send_photo(chat_id=555, photo="https://x/a.jpg", caption="short")

    assert bot.send_photo.await_count == 1
    assert bot.send_message.await_count == 0
    assert result["caption_truncated"] is False
    assert result["follow_up_message_ids"] == []


@pytest.mark.asyncio
async def test_caption_over_the_limit_truncates_and_threads_the_remainder():
    service, bot = _service_with_bot()
    caption = "A" * (_TG_CAPTION_LIMIT + 500)

    result = await service.send_photo(chat_id=555, photo="https://x/a.jpg", caption=caption)

    # The media send carries a caption within the cap...
    sent_caption = bot.send_photo.await_args.kwargs["caption"]
    assert _tg_len(sent_caption) <= _TG_CAPTION_LIMIT
    # ...and the remainder is threaded underneath it as a reply.
    assert bot.send_message.await_count == 1
    spill = bot.send_message.await_args.kwargs
    assert spill["reply_to_message_id"] == 100
    assert spill["disable_notification"] is True
    assert result["caption_truncated"] is True
    assert result["follow_up_message_ids"] == [200]


@pytest.mark.asyncio
async def test_caption_is_split_before_markdown_conversion():
    """Truncating after markdown->HTML could orphan a <b> from its closer.

    Splitting the raw body keeps every emitted part well-formed.
    """
    service, bot = _service_with_bot()
    # Sized so the split point falls *inside* the bold span: 1000 chars of
    # filler puts the cut in the middle of "**bold spanning the cut**".
    caption = ("word " * 200) + "**bold spanning the cut**"

    await service.send_photo(
        chat_id=555, photo="https://x/a.jpg", caption=caption, parse_mode="Auto"
    )

    head = bot.send_photo.await_args.kwargs["caption"] or ""
    tail = bot.send_message.await_args.kwargs["text"] or ""

    # The marker really is straddled — otherwise this test proves nothing.
    assert "bold" in head and "cut" in tail
    # Neither part may carry a dangling tag. Converting first and splitting
    # after would leave "<b>" open in the head and "</b>" orphaned in the tail,
    # which Telegram rejects with "can't find end of the entity".
    for part in (head, tail):
        assert part.count("<b>") == part.count("</b>")


@pytest.mark.asyncio
async def test_document_caption_spills_through_the_same_path():
    service, bot = _service_with_bot()
    result = await service.send_document(
        chat_id=555, document="https://x/a.pdf", caption="B" * (_TG_CAPTION_LIMIT + 10)
    )

    assert bot.send_document.await_count == 1
    assert bot.send_message.await_count == 1
    assert result["caption_truncated"] is True


# ---------------------------------------------------------------------------
# inbound formatting
# ---------------------------------------------------------------------------


def test_animation_is_not_classified_as_document():
    """Telegram sets .document on animation messages too.

    Probing document first made every GIF arrive as a document, dropping the
    animation metadata.
    """
    msg = _message(
        animation=SimpleNamespace(
            file_id="ANIM",
            file_unique_id="uniq",
            width=320,
            height=240,
            duration=3,
            file_name="cat.gif",
            mime_type="video/mp4",
            file_size=1024,
        ),
        document=SimpleNamespace(
            file_id="DOC",
            file_unique_id="uniq",
            file_name="cat.gif",
            mime_type="image/gif",
            file_size=1024,
        ),
    )
    assert _detect_content_type(msg) == "animation"

    data = TelegramService()._format_message(msg)
    assert data["content_type"] == "animation"
    assert data["media"]["file_id"] == "ANIM"


@pytest.mark.parametrize(
    "field,obj,kind",
    [
        (
            "voice",
            SimpleNamespace(
                file_id="V", file_unique_id="uv", duration=5, mime_type=None, file_size=99
            ),
            "voice",
        ),
        (
            "video",
            SimpleNamespace(
                file_id="VD",
                file_unique_id="uvd",
                width=1,
                height=2,
                duration=4,
                file_name="v.mp4",
                mime_type="video/mp4",
                file_size=10,
            ),
            "video",
        ),
        (
            "sticker",
            SimpleNamespace(
                file_id="S",
                file_unique_id="us",
                width=1,
                height=1,
                emoji="\U0001f600",
                set_name="pack",
                is_animated=False,
                is_video=False,
                file_size=7,
            ),
            "sticker",
        ),
        (
            "video_note",
            SimpleNamespace(
                file_id="VN", file_unique_id="uvn", length=240, duration=6, file_size=11
            ),
            "video_note",
        ),
        (
            "audio",
            SimpleNamespace(
                file_id="A",
                file_unique_id="ua",
                duration=8,
                performer="p",
                title="t",
                file_name="a.mp3",
                mime_type="audio/mpeg",
                file_size=12,
            ),
            "audio",
        ),
    ],
)
def test_every_media_kind_is_extracted_and_normalized(field, obj, kind):
    """Before this, only photo/document/location/contact survived —
    video, audio, voice, sticker and poll file_ids were dropped."""
    data = TelegramService()._format_message(_message(**{field: obj}))

    assert data["content_type"] == kind
    assert data[kind]["file_id"] == obj.file_id
    assert data["has_media"] is True

    media = data["media"]
    assert media["kind"] == kind
    assert media["file_id"] == obj.file_id
    assert media["mime_type"], "mime_type must be synthesised when Telegram omits it"
    assert media["file_name"], "file_name must be synthesised when Telegram omits it"
    # The trigger never downloads; media travels as an id, not bytes.
    assert media["file_path"] is None
    assert media["downloaded"] is False
    assert "data" not in media and "base64" not in media


def test_voice_gets_a_synthesised_ogg_mime_and_name():
    voice = SimpleNamespace(
        file_id="V", file_unique_id="uv", duration=5, mime_type=None, file_size=99
    )
    media = TelegramService()._format_message(_message(voice=voice))["media"]
    assert media["mime_type"] == "audio/ogg"
    assert media["file_name"].endswith(".ogg")


def test_animated_and_video_stickers_get_distinct_mimes():
    def sticker(**kw):
        return SimpleNamespace(
            file_id="S",
            file_unique_id="us",
            width=1,
            height=1,
            emoji=None,
            set_name=None,
            is_animated=False,
            is_video=False,
            file_size=1,
            **kw,
        )

    plain = TelegramService()._format_message(_message(sticker=sticker()))["media"]
    assert plain["mime_type"] == "image/webp"

    animated = sticker()
    animated.is_animated = True
    assert (
        TelegramService()._format_message(_message(sticker=animated))["media"]["mime_type"]
        == "application/x-tgsticker"
    )


def test_poll_and_location_details_are_captured():
    poll = SimpleNamespace(
        id="p1",
        question="Which?",
        options=[SimpleNamespace(text="a"), SimpleNamespace(text="b")],
        type="regular",
        is_anonymous=True,
        allows_multiple_answers=False,
        is_closed=False,
        total_voter_count=3,
    )
    data = TelegramService()._format_message(_message(poll=poll))
    assert data["content_type"] == "poll"
    assert data["poll"]["options"] == ["a", "b"]
    # A poll is not a downloadable file.
    assert data["has_media"] is False
    assert data["media"] is None

    location = SimpleNamespace(
        latitude=1.5, longitude=2.5, horizontal_accuracy=None, live_period=None
    )
    loc = TelegramService()._format_message(_message(location=location))
    assert loc["location"]["latitude"] == 1.5


def test_text_still_falls_back_to_caption():
    """Back-compat lock: workflows read `text` on photo messages."""
    photo = SimpleNamespace(
        file_id="P", file_unique_id="up", width=1, height=1, file_size=5
    )
    data = TelegramService()._format_message(_message(photo=[photo], caption="a caption"))

    assert data["text"] == "a caption"  # unchanged historical behaviour
    assert data["caption"] == "a caption"  # new, additive
    assert data["media"]["caption"] == "a caption"


def test_reply_to_carries_quoted_media():
    quoted = _message(
        message_id=42,
        voice=SimpleNamespace(
            file_id="QV", file_unique_id="uqv", duration=2, mime_type=None, file_size=3
        ),
    )
    data = TelegramService()._format_message(_message(text="transcribe this", reply_to_message=quoted))

    assert data["reply_to_message_id"] == 42
    assert data["reply_to"]["content_type"] == "voice"
    assert data["reply_to"]["media"]["file_id"] == "QV"


def test_media_group_id_is_exposed_for_album_correlation():
    photo = SimpleNamespace(file_id="P", file_unique_id="up", width=1, height=1, file_size=5)
    data = TelegramService()._format_message(_message(photo=[photo], media_group_id="alb-1"))
    assert data["media_group_id"] == "alb-1"


def test_detail_extraction_failure_does_not_drop_the_message():
    """A partial provider payload must not stop the event reaching the workflow."""
    broken = SimpleNamespace(file_id="V")  # missing every other attribute
    data = TelegramService()._format_message(_message(voice=broken))

    assert data["content_type"] == "voice"
    assert data["message_id"] == 1  # the message still came through
    assert data.get("voice") is None


# ---------------------------------------------------------------------------
# polling error severity
# ---------------------------------------------------------------------------


def test_conflict_is_a_throttled_warning_not_an_error(caplog):
    """A second getUpdates consumer is self-healing, so it must not read as
    fatal — and it must not re-log on every ~6s retry."""
    from telegram.error import Conflict

    service = TelegramService()
    with caplog.at_level("DEBUG", logger="nodes.telegram._service"):
        for _ in range(25):
            service._on_polling_error(Conflict("terminated by other getUpdates request"))

    conflict_records = [r for r in caplog.records if "getUpdates consumer" in r.message]
    assert len(conflict_records) == 1, "25 retries must collapse to one line"
    assert conflict_records[0].levelname == "WARNING"
    # The operator needs to know it recovers by itself and that sending works.
    assert "recovers" in conflict_records[0].message
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


def test_suppressed_conflicts_are_counted_on_the_next_emission(caplog, monkeypatch):
    from telegram.error import Conflict
    from nodes.telegram import _service as mod

    service = TelegramService()
    clock = {"t": 1000.0}
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock["t"])

    with caplog.at_level("DEBUG", logger="nodes.telegram._service"):
        for _ in range(5):
            service._on_polling_error(Conflict("x"))
        clock["t"] += mod._POLLING_WARN_INTERVAL_S + 1
        service._on_polling_error(Conflict("x"))

    lines = [r.message for r in caplog.records if "getUpdates consumer" in r.message]
    assert len(lines) == 2
    assert "repeated 4x" in lines[1]


def test_network_and_rate_limit_stay_debug(caplog):
    from telegram.error import NetworkError, RetryAfter

    service = TelegramService()
    with caplog.at_level("DEBUG", logger="nodes.telegram._service"):
        service._on_polling_error(NetworkError("connection reset"))
        service._on_polling_error(RetryAfter(30))

    assert not [r for r in caplog.records if r.levelname in ("WARNING", "ERROR")]


def test_unexpected_polling_errors_still_log_at_error(caplog):
    service = TelegramService()
    with caplog.at_level("DEBUG", logger="nodes.telegram._service"):
        service._on_polling_error(RuntimeError("something genuinely broken"))

    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    assert "something genuinely broken" in errors[0].message


def test_transient_and_fatal_polling_messages_are_distinguishable():
    """Both paths used to emit the identical '[Telegram] Polling error:'
    string despite opposite consequences — one retries, the other marks the
    bot disconnected."""
    import inspect

    from nodes.telegram._service import TelegramService as Svc

    callback_src = inspect.getsource(Svc._on_polling_error)
    loop_src = inspect.getsource(Svc._run_polling)
    assert "bot disconnected" in loop_src
    assert "bot disconnected" not in callback_src


@pytest.mark.asyncio
async def test_polling_keeps_the_messages_telegram_held_during_a_restart():
    """Telegram holds updates for 24 hours. Polling used to start with
    ``drop_pending_updates=True``, which discarded every message sent while
    the backend was down (RFC-0005 Phase 0.5)."""
    import asyncio

    service = TelegramService()
    started = asyncio.Event()
    updater = SimpleNamespace(start_polling=AsyncMock(side_effect=lambda **_: started.set()))
    service._application = SimpleNamespace(start=AsyncMock(), updater=updater)

    task = asyncio.create_task(service._run_polling())
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert updater.start_polling.await_args.kwargs["drop_pending_updates"] is False


# ---------------------------------------------------------------------------
# shutdown: release the getUpdates slot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_hook_releases_the_polling_slot(monkeypatch):
    """Telegram reserves the single getUpdates slot until an in-flight long
    poll times out (~30s). Without a shutdown hook the process died holding
    it, so the next start collided with its own predecessor and logged a
    Conflict for the whole drain window.
    """
    import nodes.telegram as tg
    from services.plugin.shutdown_hooks import run_shutdown_hooks

    service = MagicMock()
    service.connected = True
    service.disconnect = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(tg, "get_telegram_service", lambda: service)

    await run_shutdown_hooks()

    service.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_shutdown_hook_is_a_noop_when_not_connected(monkeypatch):
    import nodes.telegram as tg
    from services.plugin.shutdown_hooks import run_shutdown_hooks

    service = MagicMock()
    service.connected = False
    service.disconnect = AsyncMock()
    monkeypatch.setattr(tg, "get_telegram_service", lambda: service)

    await run_shutdown_hooks()

    service.disconnect.assert_not_awaited()


def test_telegram_registers_a_shutdown_hook():
    """Locks the registration itself — the hook body being correct is
    useless if nobody wires it into lifespan teardown."""
    import nodes.telegram  # noqa: F401  (import registers)
    from services.plugin.shutdown_hooks import registered_labels

    assert "telegram" in registered_labels()


def test_media_content_types_covers_every_downloadable_kind():
    assert _MEDIA_CONTENT_TYPES == {
        "photo",
        "video",
        "audio",
        "voice",
        "animation",
        "video_note",
        "sticker",
        "document",
    }
