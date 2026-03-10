"""Interactive tool approval via Telegram inline buttons.

Used exclusively in plan-execute mode: after Claude produces a plan, the user
clicks [▶ Execute] and subsequent write tool calls are approved interactively.
"""

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import structlog
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

logger = structlog.get_logger()

# Tools that pause for user approval when approval_manager is active.
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "Bash", "Task", "NotebookEdit"}

# Tools that are always auto-approved (read-only operations).
READ_TOOLS = {
    "Read",
    "Grep",
    "Glob",
    "LS",
    "WebFetch",
    "WebSearch",
    "Skill",
    "TodoRead",
    "TodoWrite",
}


@dataclass
class PendingApproval:
    """A single in-flight tool approval request."""

    request_id: str
    tool_name: str
    tool_input: Dict[str, Any]
    event: asyncio.Event = field(default_factory=asyncio.Event)
    result: Optional[str] = None  # "allow" | "deny" | "allow_all"
    message_id: Optional[int] = None


class ToolApprovalManager:
    """Manage interactive tool approval via Telegram inline buttons.

    Lifecycle:
        1. Created just before ``run_command`` in plan-execute flow.
        2. Stored in ``context.user_data["_approval_manager"]`` so the
           callback handler can reach it.
        3. ``request_approval`` is called from inside ``can_use_tool``
           (async, SDK awaits it).
        4. Callback handler calls ``resolve`` when user clicks a button.
        5. Cleaned up from ``user_data`` after ``run_command`` returns.
    """

    def __init__(self, bot: Bot, chat_id: int, timeout: float = 120.0) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.timeout = timeout
        self.pending: Dict[str, PendingApproval] = {}
        self.allow_all: bool = False  # True after user clicks "Allow All"

    async def request_approval(self, tool_name: str, tool_input: Dict[str, Any]) -> str:
        """Send a Telegram approval request and wait for the user's decision.

        Returns ``"allow"`` or ``"deny"``.
        """
        # If user previously clicked "Allow All", skip further prompts.
        if self.allow_all:
            logger.debug("Auto-allowing tool (allow_all active)", tool_name=tool_name)
            return "allow"

        # Non-write tools are always allowed without prompting.
        if tool_name not in WRITE_TOOLS:
            return "allow"

        request_id = uuid.uuid4().hex[:8]
        pending = PendingApproval(
            request_id=request_id,
            tool_name=tool_name,
            tool_input=tool_input,
        )
        self.pending[request_id] = pending

        detail = self._format_tool_detail(tool_name, tool_input)

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Allow",
                        callback_data=f"perm:{request_id}:allow",
                    ),
                    InlineKeyboardButton(
                        "❌ Deny",
                        callback_data=f"perm:{request_id}:deny",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "✅ Allow All (this session)",
                        callback_data=f"perm:{request_id}:allow_all",
                    ),
                ],
            ]
        )

        msg = await self.bot.send_message(
            chat_id=self.chat_id,
            text=(
                f"🔐 <b>Permission Request</b>\n\n"
                f"Claude wants to run <b>{tool_name}</b>:\n"
                f"<code>{detail}</code>\n\n"
                f"⏱ Auto-deny in {int(self.timeout)}s"
            ),
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        pending.message_id = msg.message_id

        logger.info(
            "Sent tool approval request",
            tool_name=tool_name,
            request_id=request_id,
            chat_id=self.chat_id,
        )

        try:
            await asyncio.wait_for(pending.event.wait(), timeout=self.timeout)
        except asyncio.TimeoutError:
            pending.result = "deny"
            logger.info(
                "Tool approval timed out — denying",
                tool_name=tool_name,
                request_id=request_id,
            )
            try:
                await self.bot.edit_message_text(
                    chat_id=self.chat_id,
                    message_id=msg.message_id,
                    text=(
                        f"🔐 <b>Permission Request</b> — ⏰ <i>Timed out (denied)</i>\n\n"
                        f"Claude wanted to run <b>{tool_name}</b>:\n"
                        f"<code>{detail}</code>"
                    ),
                    parse_mode="HTML",
                )
            except Exception:
                pass
        finally:
            self.pending.pop(request_id, None)

        decision = pending.result or "deny"

        if decision == "allow_all":
            self.allow_all = True
            decision = "allow"

        logger.info(
            "Tool approval resolved",
            tool_name=tool_name,
            request_id=request_id,
            decision=decision,
        )
        return decision

    def resolve(self, request_id: str, decision: str) -> bool:
        """Resolve a pending approval from the callback handler.

        Returns True if the request was found and resolved, False if it had
        already expired or never existed.
        """
        pending = self.pending.get(request_id)
        if not pending:
            return False
        pending.result = decision
        pending.event.set()
        return True

    def _format_tool_detail(self, tool_name: str, tool_input: Dict[str, Any]) -> str:
        """Format tool input into a human-readable summary."""
        import html

        if tool_name in ("Write", "Edit", "MultiEdit"):
            path = tool_input.get("file_path") or tool_input.get("path", "?")
            return html.escape(str(path))
        if tool_name == "Bash":
            cmd = str(tool_input.get("command", "?"))
            if len(cmd) > 300:
                cmd = cmd[:300] + "…"
            return html.escape(cmd)
        if tool_name == "Task":
            desc = str(tool_input.get("description", "?"))
            if len(desc) > 300:
                desc = desc[:300] + "…"
            return html.escape(desc)
        # Fallback: dump the whole input dict, truncated.
        raw = str(tool_input)
        if len(raw) > 300:
            raw = raw[:300] + "…"
        return html.escape(raw)
