"""Tests for topic-aware StreamingProgress fallbacks."""

from unittest.mock import AsyncMock, MagicMock

from src.bot.utils.streaming_progress import StreamingProgress


async def test_typing_heartbeat_passes_message_thread_id():
    """Typing heartbeat must stay in the originating topic."""
    chat = MagicMock()
    chat.send_action = AsyncMock(return_value=True)

    progress = StreamingProgress(
        chat=chat,
        bot=MagicMock(),
        chat_id=123,
        message_thread_id=777,
        verbose_level=1,
        enable_drafts=False,
        draft_interval=0.3,
    )
    progress._typing_stop.set()

    await progress._typing_heartbeat()

    chat.send_action.assert_awaited_once_with("typing", message_thread_id=777)


async def test_ensure_progress_message_passes_message_thread_id():
    """Lazy-created progress messages must stay in the originating topic."""
    progress_message = MagicMock()
    chat = MagicMock()
    chat.send_message = AsyncMock(return_value=progress_message)

    progress = StreamingProgress(
        chat=chat,
        bot=MagicMock(),
        chat_id=123,
        message_thread_id=777,
        verbose_level=1,
        enable_drafts=False,
        draft_interval=0.3,
    )

    result = await progress._ensure_progress_msg()

    assert result is progress_message
    chat.send_message.assert_awaited_once_with(
        text="🔧 ...",
        parse_mode="HTML",
        message_thread_id=777,
    )
