"""Tests for topic-aware media replies in message handlers."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.bot.handlers.message import handle_text_message
from src.bot.utils import formatting as formatting_module
from src.config import create_test_config


def _install_formatter(monkeypatch, formatted_messages):
    class FakeResponseFormatter:
        def __init__(self, settings):
            self.settings = settings

        def format_claude_response(self, content, context=None):
            return formatted_messages

    monkeypatch.setattr(
        formatting_module,
        "ResponseFormatter",
        FakeResponseFormatter,
    )


def _build_update(message_thread_id: int) -> MagicMock:
    progress_message = MagicMock()
    progress_message.edit_text = AsyncMock()
    progress_message.delete = AsyncMock()

    chat = MagicMock()
    chat.send_action = AsyncMock(return_value=True)
    chat.send_message = AsyncMock(return_value=progress_message)
    chat.send_media_group = AsyncMock(return_value=())

    message = MagicMock()
    message.text = "show me images"
    message.message_id = 1234
    message.message_thread_id = message_thread_id
    message.direct_messages_topic = None
    message.chat = chat
    message.chat_id = -1001
    message.reply_text = AsyncMock()
    message.reply_photo = AsyncMock()
    message.reply_document = AsyncMock()

    update = MagicMock()
    update.message = message
    update.effective_message = message
    update.effective_chat = chat
    update.effective_user.id = 42
    return update


def _build_context(settings, run_command) -> MagicMock:
    claude_integration = MagicMock()
    claude_integration.run_command = AsyncMock(side_effect=run_command)

    context = MagicMock()
    context.bot = MagicMock()
    context.bot_data = {
        "settings": settings,
        "rate_limiter": None,
        "audit_logger": None,
        "claude_integration": claude_integration,
        "storage": None,
    }
    context.user_data = {}
    return context


def _tool_call_update(*paths: Path) -> SimpleNamespace:
    return SimpleNamespace(
        type="assistant",
        tool_calls=[
            {
                "name": "send_image_to_user",
                "input": {"file_path": str(path), "caption": ""},
            }
            for path in paths
        ],
        content="",
    )


async def test_handle_text_message_caption_album_keeps_thread_context(
    tmp_path: Path, monkeypatch
):
    """Captioned photo albums must reply inside the source topic."""
    settings = create_test_config(
        approved_directory=str(tmp_path),
        enable_stream_drafts=False,
    )
    image_a = tmp_path / "a.jpg"
    image_b = tmp_path / "b.jpg"
    image_a.write_bytes(b"a")
    image_b.write_bytes(b"b")

    formatted_messages = [
        SimpleNamespace(text="Short caption", parse_mode="HTML", reply_markup=None)
    ]
    _install_formatter(monkeypatch, formatted_messages)

    async def run_command(**kwargs):
        await kwargs["on_stream"](_tool_call_update(image_a, image_b))
        return SimpleNamespace(
            content="ignored",
            session_id="session-1",
            tools_used=[],
            is_error=False,
        )

    update = _build_update(message_thread_id=777)
    context = _build_context(settings, run_command)

    await handle_text_message(update, context)

    send_kwargs = update.message.chat.send_media_group.call_args.kwargs
    assert send_kwargs["message_thread_id"] == 777
    assert send_kwargs["reply_to_message_id"] == 1234


async def test_handle_text_message_separate_album_keeps_thread_context(
    tmp_path: Path, monkeypatch
):
    """Standalone photo albums must reply inside the source topic."""
    settings = create_test_config(
        approved_directory=str(tmp_path),
        enable_stream_drafts=False,
    )
    image_a = tmp_path / "a.jpg"
    image_b = tmp_path / "b.jpg"
    image_a.write_bytes(b"a")
    image_b.write_bytes(b"b")

    formatted_messages = [
        SimpleNamespace(text="x" * 1025, parse_mode="HTML", reply_markup=None)
    ]
    _install_formatter(monkeypatch, formatted_messages)

    async def run_command(**kwargs):
        await kwargs["on_stream"](_tool_call_update(image_a, image_b))
        return SimpleNamespace(
            content="ignored",
            session_id="session-1",
            tools_used=[],
            is_error=False,
        )

    update = _build_update(message_thread_id=777)
    context = _build_context(settings, run_command)

    await handle_text_message(update, context)

    send_kwargs = update.message.chat.send_media_group.call_args.kwargs
    assert send_kwargs["message_thread_id"] == 777
    assert send_kwargs["reply_to_message_id"] == 1234
