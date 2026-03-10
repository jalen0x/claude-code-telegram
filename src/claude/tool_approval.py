"""Real-time interactive tool approval for agentic mode.

When Claude attempts a write operation the bot sends a Telegram message with
inline approval buttons and pauses execution until the user responds or the
request times out.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import structlog
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

logger = structlog.get_logger()

# Tools that mutate state and therefore require user approval in "ask" mode
_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "Bash", "Task", "NotebookEdit"}



def _escape_html(text: str) -> str:
    """Minimal HTML escaping for Telegram HTML parse mode."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _format_tool_summary(tool_name: str, tool_input: Dict[str, Any]) -> str:
    """Return a concise HTML-formatted description of a pending tool call."""
    lines = [f"Tool: <code>{_escape_html(tool_name)}</code>"]

    if tool_name in ("Write", "Edit", "MultiEdit"):
        file_path = str(tool_input.get("file_path") or tool_input.get("path", ""))
        if file_path:
            lines.append(f"File: <code>{_escape_html(file_path)}</code>")

    elif tool_name == "Bash":
        command = str(tool_input.get("command", ""))
        if command:
            if len(command) > 120:
                command = command[:117] + "..."
            lines.append(f"Command: <code>{_escape_html(command)}</code>")

    elif tool_name == "Task":
        description = str(
            tool_input.get("description") or tool_input.get("prompt", "")
        )
        if description:
            if len(description) > 100:
                description = description[:97] + "..."
            lines.append(f"Task: {_escape_html(description)}")

    elif tool_name == "NotebookEdit":
        file_path = str(tool_input.get("notebook_path", ""))
        if file_path:
            lines.append(f"Notebook: <code>{_escape_html(file_path)}</code>")

    return "\n".join(lines)


@dataclass
class PendingApproval:
    """State for a single pending tool approval request."""

    event: asyncio.Event
    decision: str = "deny"  # default: auto-deny on timeout
    message_id: Optional[int] = None


class ToolApprovalManager:
    """Manages real-time per-tool approval requests via Telegram inline buttons.

    Lifecycle
    ---------
    1. Instantiate before ``run_command()``:
       ``manager = ToolApprovalManager(bot=bot, chat_id=chat_id)``
    2. Pass to ``run_command()``; the SDK wires it into ``can_use_tool``.
    3. When a write tool fires, the SDK calls
       ``decision = await manager.request_approval(tool_name, tool_input)``.
    4. The Telegram callback handler resolves the request:
       ``manager.resolve(approval_id, decision)``
    5. On session end (or error), call ``await manager.cleanup()``.

    Decisions
    ---------
    - ``"allow"``     – execute this tool
    - ``"deny"``      – block this tool (also the timeout default)
    - ``"allow_all"`` – execute this tool and auto-allow all future write tools
    """

    def __init__(self, bot: Bot, chat_id: int) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._pending: Dict[str, PendingApproval] = {}
        self._allow_all: bool = False
        self._counter: int = 0

    def is_write_tool(self, tool_name: str) -> bool:
        """Return True when the tool requires user approval."""
        return tool_name in _WRITE_TOOLS

    async def request_approval(
        self, tool_name: str, tool_input: Dict[str, Any]
    ) -> str:
        """Request user approval for a single tool call.

        Blocks (asynchronously) until the user clicks a button.
        Returns one of: ``"allow"``, ``"deny"``, ``"allow_all"``.
        """
        # Fast paths — no Telegram message needed
        if self._allow_all or not self.is_write_tool(tool_name):
            return "allow"

        approval_id = str(self._counter)
        self._counter += 1

        pending = PendingApproval(event=asyncio.Event())
        self._pending[approval_id] = pending

        summary = _format_tool_summary(tool_name, tool_input)

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Allow",
                        callback_data=f"tool_approval:{approval_id}:allow",
                    ),
                    InlineKeyboardButton(
                        "❌ Deny",
                        callback_data=f"tool_approval:{approval_id}:deny",
                    ),
                    InlineKeyboardButton(
                        "✅✅ Allow All",
                        callback_data=f"tool_approval:{approval_id}:allow_all",
                    ),
                ]
            ]
        )

        try:
            sent = await self._bot.send_message(
                chat_id=self._chat_id,
                text=f"🔧 <b>Tool Request</b>\n{summary}",
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            pending.message_id = sent.message_id
        except Exception as exc:
            logger.warning(
                "Failed to send approval request — denying tool call",
                tool_name=tool_name,
                error=str(exc),
            )
            self._pending.pop(approval_id, None)
            return "deny"

        await pending.event.wait()

        entry = self._pending.pop(approval_id, None)
        return entry.decision if entry else "deny"

    def resolve(self, approval_id: str, decision: str) -> None:
        """Resolve a pending approval (called by the Telegram callback handler).

        Args:
            approval_id: Identifier of the pending approval to resolve.
            decision:    One of ``"allow"``, ``"deny"``, ``"allow_all"``.
        """
        pending = self._pending.get(approval_id)
        if pending is None:
            logger.warning(
                "Attempted to resolve unknown or already-expired approval",
                approval_id=approval_id,
                decision=decision,
            )
            return

        pending.decision = decision
        if decision == "allow_all":
            self._allow_all = True
        pending.event.set()

    async def cleanup(self) -> None:
        """Cancel all pending approvals.

        Call this when the session ends or the bot shuts down to release any
        coroutines blocked inside ``request_approval()``.
        """
        for approval_id, pending in list(self._pending.items()):
            pending.decision = "deny"
            pending.event.set()
            if pending.message_id:
                try:
                    await self._bot.edit_message_text(
                        chat_id=self._chat_id,
                        message_id=pending.message_id,
                        text="⚠️ <i>Session ended — operation cancelled.</i>",
                        parse_mode="HTML",
                        reply_markup=None,
                    )
                except Exception:
                    pass
        self._pending.clear()
