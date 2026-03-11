"""Unified streaming progress for all Telegram handlers.

Provides an async context manager that owns the full streaming lifecycle:
typing heartbeat, DraftStreamer integration, lazy progress messages, and
cleanup — so every handler gets the same streaming UX without duplicating
~120 lines of boilerplate.
"""

import asyncio
from typing import Any, Callable, Coroutine, List, Optional

import structlog
import telegram

from ..utils.html_format import escape_html

logger = structlog.get_logger()

# Max chars for the accumulated progress message (stay under Telegram's 4096).
_MAX_PROGRESS_CHARS = 3000


def _extract_tool_detail(name: str, inp: dict) -> str:  # type: ignore[type-arg]
    """Extract a human-readable detail snippet from a tool call's input."""
    if name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        return str(inp.get("file_path") or inp.get("path", ""))
    if name == "Bash":
        return str(inp.get("command", ""))[:80]
    if name in ("Read", "Grep", "Glob"):
        return str(inp.get("file_path") or inp.get("path") or inp.get("pattern", ""))
    return ""


def _format_progress_update(
    update_obj: object, verbose_level: int = 1
) -> Optional[str]:
    """Format progress updates with enhanced context and visual indicators.

    verbose_level controls output:
      0 — suppress all progress (return None always)
      1 — tool name only, no input details
      2 — tool name + file path / command snippet (full detail)
    """
    if verbose_level == 0:
        return None

    if update_obj.type == "assistant" and update_obj.tool_calls:  # type: ignore[attr-defined]
        lines = []
        for tc in update_obj.tool_calls:  # type: ignore[attr-defined]
            name = tc.get("name", "?")
            if verbose_level >= 2:
                detail_raw = _extract_tool_detail(name, tc.get("input") or {})
                detail = (
                    f" <code>{escape_html(detail_raw)}</code>" if detail_raw else ""
                )
            else:
                detail = ""
            lines.append(f"🔧 <b>{name}</b>{detail}")
        return "\n".join(lines) if lines else None

    elif update_obj.type == "assistant" and update_obj.content:  # type: ignore[attr-defined]
        if verbose_level < 2:
            return None
        content_preview = (
            update_obj.content[:120] + "…"  # type: ignore[attr-defined]
            if len(update_obj.content) > 120  # type: ignore[attr-defined]
            else update_obj.content  # type: ignore[attr-defined]
        )
        return f"💬 <i>{escape_html(content_preview)}</i>"

    return None


# Type alias for the interceptor callback.
Interceptor = Callable[[Any], Coroutine[Any, Any, None]]


class StreamingProgress:
    """Async context manager owning the full streaming lifecycle.

    Usage::

        async with StreamingProgress(chat, bot, chat_id, ...) as sp:
            sp.set_interceptor(my_mcp_interceptor)
            response = await run_command(..., on_stream=sp.stream_callback)
    """

    def __init__(
        self,
        chat: telegram.Chat,
        bot: telegram.Bot,
        chat_id: int,
        message_thread_id: Optional[int],
        verbose_level: int,
        enable_drafts: bool,
        draft_interval: float,
    ) -> None:
        self._chat = chat
        self._bot = bot
        self._chat_id = chat_id
        self._message_thread_id = message_thread_id
        self._verbose_level = verbose_level
        self._enable_drafts = enable_drafts
        self._draft_interval = draft_interval

        self._interceptor: Optional[Interceptor] = None
        self._progress_msg: Optional[telegram.Message] = None
        self._progress_lines: List[str] = []
        self._draft_streamer: Any = None  # DraftStreamer | None
        self._typing_stop = asyncio.Event()
        self._typing_task: Optional[asyncio.Task[None]] = None

    # -- Public API ----------------------------------------------------------

    def _thread_kwargs(self) -> dict[str, int]:
        """Return thread-routing kwargs for topic-aware sends."""
        if self._message_thread_id is None:
            return {}
        return {"message_thread_id": self._message_thread_id}

    def set_interceptor(self, fn: Interceptor) -> None:
        """Set an interceptor called on every stream update (e.g. MCP images)."""
        self._interceptor = fn

    @property
    def progress_lines(self) -> List[str]:
        return self._progress_lines

    async def stream_callback(self, update_obj: Any) -> None:
        """Pass to ``on_stream=`` of ``run_command``."""
        # 1. Interceptor (e.g. MCP image collection).
        if self._interceptor is not None:
            try:
                await self._interceptor(update_obj)
            except Exception as e:
                logger.debug("Stream interceptor failed", error=str(e))

        # 2. Tool calls
        if getattr(update_obj, "tool_calls", None):
            await self._handle_tool_calls(update_obj)
            if self._draft_streamer is not None:
                return  # skip progress_msg editing when using drafts

        # 3. Text deltas → DraftStreamer only.
        if self._draft_streamer is not None:
            if not getattr(update_obj, "tool_calls", None):
                update_type = getattr(update_obj, "type", None)
                if update_type == "content_block_delta" or (
                    update_type == "assistant" and getattr(update_obj, "content", "")
                ):
                    text = getattr(update_obj, "content", "")
                    if text:
                        await self._draft_streamer.append_text(text)
            return  # skip progress_msg editing when using drafts

        # 4. Fallback: edit progress_msg with formatted line.
        try:
            new_line = _format_progress_update(
                update_obj, verbose_level=self._verbose_level
            )
            if new_line:
                self._progress_lines.append(new_line)
                combined = "\n".join(self._progress_lines)
                while (
                    len(combined) > _MAX_PROGRESS_CHARS
                    and len(self._progress_lines) > 1
                ):
                    self._progress_lines.pop(0)
                    combined = "\n".join(self._progress_lines)
                msg = await self._ensure_progress_msg()
                if msg:
                    await msg.edit_text(combined, parse_mode="HTML")
        except Exception as e:
            logger.warning("Failed to update progress message", error=str(e))

    # -- Context manager -----------------------------------------------------

    async def __aenter__(self) -> "StreamingProgress":
        # Start typing heartbeat.
        self._typing_task = asyncio.create_task(self._typing_heartbeat())

        # DraftStreamer setup (gated behind enable_drafts).
        if self._enable_drafts:
            from ..utils.draft_streamer import DraftStreamer, generate_draft_id

            self._draft_streamer = DraftStreamer(
                bot=self._bot,
                chat_id=self._chat_id,
                draft_id=generate_draft_id(),
                message_thread_id=self._message_thread_id,
                throttle_interval=self._draft_interval,
            )

        # No eager progress message — created lazily on first tool call.
        self._progress_msg = None
        return self

    async def __aexit__(
        self,
        exc_type: Optional[type],
        exc_val: Optional[BaseException],
        exc_tb: Optional[Any],
    ) -> None:
        # 1. Stop typing heartbeat.
        self._typing_stop.set()
        if self._typing_task:
            self._typing_task.cancel()

        # 2. DraftStreamer: flush.
        if self._draft_streamer is not None:
            try:
                await self._draft_streamer.flush()
            except Exception:
                pass
            # Delete the progress_msg when drafts handled the UX.
            if self._progress_msg is not None:
                try:
                    await self._progress_msg.delete()
                except Exception:
                    pass
        elif self._progress_lines:
            # Keep the progress bubble as context, add a transition line.
            try:
                combined = "\n".join(self._progress_lines)
                if self._progress_msg is None:
                    return
                await self._progress_msg.edit_text(
                    combined + "\n\n💬 <i>Responding…</i>",
                    parse_mode="HTML",
                )
            except Exception:
                if self._progress_msg is not None:
                    try:
                        await self._progress_msg.delete()
                    except Exception:
                        pass
        elif self._progress_msg is not None:
            # Empty progress message with no lines — shouldn't happen, but clean up.
            try:
                await self._progress_msg.delete()
            except Exception:
                pass

    # -- Internal helpers ----------------------------------------------------

    async def _typing_heartbeat(self) -> None:
        """Send typing action immediately and refresh every 4s."""
        try:
            await self._chat.send_action("typing", **self._thread_kwargs())
        except Exception:
            pass
        while not self._typing_stop.is_set():
            try:
                await asyncio.wait_for(self._typing_stop.wait(), timeout=4.0)
            except asyncio.TimeoutError:
                pass
            if self._typing_stop.is_set():
                break
            try:
                await self._chat.send_action("typing", **self._thread_kwargs())
            except Exception:
                pass

    async def _ensure_progress_msg(self) -> Optional[telegram.Message]:
        """Lazy-create the progress message on first tool call."""
        if self._progress_msg is None:
            try:
                self._progress_msg = await self._chat.send_message(
                    text="🔧 ...",
                    parse_mode="HTML",
                    **self._thread_kwargs(),
                )
            except Exception:
                pass
        return self._progress_msg

    async def _handle_tool_calls(self, update_obj: Any) -> None:
        """Process tool calls into DraftStreamer or progress lines."""
        for tc in update_obj.tool_calls:
            tc_name = tc.get("name", "")
            if self._draft_streamer is not None and self._verbose_level >= 1:
                detail_raw = (
                    _extract_tool_detail(tc_name, tc.get("input") or {})
                    if self._verbose_level >= 2
                    else ""
                )
                detail = f" {detail_raw}" if detail_raw else ""
                await self._draft_streamer.append_tool(f"🔧 {tc_name}{detail}")
