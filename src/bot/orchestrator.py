"""Message orchestrator — single entry point for all Telegram updates.

Delegates to the classic handler set (commands, message, callback handlers)
for all Telegram interactions.
"""

import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import structlog
from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ..claude.tool_approval import ToolApprovalManager
from ..config.settings import Settings

logger = structlog.get_logger()

# Patterns that look like secrets/credentials in CLI arguments
_SECRET_PATTERNS: List[re.Pattern[str]] = [
    # API keys / tokens (sk-ant-..., sk-..., ghp_..., gho_..., github_pat_..., xoxb-...)
    re.compile(
        r"(sk-ant-api\d*-[A-Za-z0-9_-]{10})[A-Za-z0-9_-]*"
        r"|(sk-[A-Za-z0-9_-]{20})[A-Za-z0-9_-]*"
        r"|(ghp_[A-Za-z0-9]{5})[A-Za-z0-9]*"
        r"|(gho_[A-Za-z0-9]{5})[A-Za-z0-9]*"
        r"|(github_pat_[A-Za-z0-9_]{5})[A-Za-z0-9_]*"
        r"|(xoxb-[A-Za-z0-9]{5})[A-Za-z0-9-]*"
    ),
    # AWS access keys
    re.compile(r"(AKIA[0-9A-Z]{4})[0-9A-Z]{12}"),
    # Generic long hex/base64 tokens after common flags/env patterns
    re.compile(
        r"((?:--token|--secret|--password|--api-key|--apikey|--auth)"
        r"[= ]+)['\"]?[A-Za-z0-9+/_.:-]{8,}['\"]?"
    ),
    # Inline env assignments like KEY=value
    re.compile(
        r"((?:TOKEN|SECRET|PASSWORD|API_KEY|APIKEY|AUTH_TOKEN|PRIVATE_KEY"
        r"|ACCESS_KEY|CLIENT_SECRET|WEBHOOK_SECRET)"
        r"=)['\"]?[^\s'\"]{8,}['\"]?"
    ),
    # Bearer / Basic auth headers
    re.compile(r"(Bearer )[A-Za-z0-9+/_.:-]{8,}" r"|(Basic )[A-Za-z0-9+/=]{8,}"),
    # Connection strings with credentials  user:pass@host
    re.compile(r"://([^:]+:)[^@]{4,}(@)"),
]


def _redact_secrets(text: str) -> str:
    """Replace likely secrets/credentials with redacted placeholders."""
    result = text
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(
            lambda m: next((g + "***" for g in m.groups() if g is not None), "***"),
            result,
        )
    return result


class MessageOrchestrator:
    """Single entry point for all Telegram updates."""

    def __init__(self, settings: Settings, deps: Dict[str, Any]):
        self.settings = settings
        self.deps = deps

    def _inject_deps(self, handler: Callable) -> Callable:  # type: ignore[type-arg]
        """Wrap handler to inject dependencies into context.bot_data."""

        async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            for key, value in self.deps.items():
                context.bot_data[key] = value
            context.bot_data["settings"] = self.settings
            context.user_data.pop("_thread_context", None)

            is_sync_bypass = handler.__name__ == "sync_threads"
            is_start_bypass = handler.__name__ == "start_command"
            message_thread_id = self._extract_message_thread_id(update)
            should_enforce = self.settings.enable_project_threads

            if should_enforce:
                if self.settings.project_threads_mode == "private":
                    should_enforce = not is_sync_bypass and not (
                        is_start_bypass and message_thread_id is None
                    )
                else:
                    should_enforce = not is_sync_bypass

            if should_enforce:
                allowed = await self._apply_thread_routing_context(update, context)
                if not allowed:
                    return

            try:
                await handler(update, context)
            finally:
                if should_enforce:
                    self._persist_thread_state(context)

        return wrapped

    async def _apply_thread_routing_context(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> bool:
        """Enforce strict project-thread routing and load thread-local state."""
        manager = context.bot_data.get("project_threads_manager")
        if manager is None:
            await self._reject_for_thread_mode(
                update,
                "❌ <b>Project Thread Mode Misconfigured</b>\n\n"
                "Thread manager is not initialized.",
            )
            return False

        chat = update.effective_chat
        message = update.effective_message
        if not chat or not message:
            return False

        if self.settings.project_threads_mode == "group":
            if chat.id != self.settings.project_threads_chat_id:
                await self._reject_for_thread_mode(
                    update,
                    manager.guidance_message(mode=self.settings.project_threads_mode),
                )
                return False
        else:
            if getattr(chat, "type", "") != "private":
                await self._reject_for_thread_mode(
                    update,
                    manager.guidance_message(mode=self.settings.project_threads_mode),
                )
                return False

        message_thread_id = self._extract_message_thread_id(update)
        if not message_thread_id:
            await self._reject_for_thread_mode(
                update,
                manager.guidance_message(mode=self.settings.project_threads_mode),
            )
            return False

        project = await manager.resolve_project(chat.id, message_thread_id)
        if not project:
            await self._reject_for_thread_mode(
                update,
                manager.guidance_message(mode=self.settings.project_threads_mode),
            )
            return False

        state_key = f"{chat.id}:{message_thread_id}"
        thread_states = context.user_data.setdefault("thread_state", {})
        state = thread_states.get(state_key, {})

        project_root = project.absolute_path
        current_dir_raw = state.get("current_directory")
        current_dir = (
            Path(current_dir_raw).resolve() if current_dir_raw else project_root
        )
        if not self._is_within(current_dir, project_root) or not current_dir.is_dir():
            current_dir = project_root

        context.user_data["current_directory"] = current_dir
        context.user_data["claude_session_id"] = state.get("claude_session_id")
        context.user_data["_thread_context"] = {
            "chat_id": chat.id,
            "message_thread_id": message_thread_id,
            "state_key": state_key,
            "project_slug": project.slug,
            "project_root": str(project_root),
            "project_name": project.name,
        }
        return True

    def _persist_thread_state(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Persist compatibility keys back into per-thread state."""
        thread_context = context.user_data.get("_thread_context")
        if not thread_context:
            return

        project_root = Path(thread_context["project_root"])
        current_dir = context.user_data.get("current_directory", project_root)
        if not isinstance(current_dir, Path):
            current_dir = Path(str(current_dir))
        current_dir = current_dir.resolve()
        if not self._is_within(current_dir, project_root) or not current_dir.is_dir():
            current_dir = project_root

        thread_states = context.user_data.setdefault("thread_state", {})
        thread_states[thread_context["state_key"]] = {
            "current_directory": str(current_dir),
            "claude_session_id": context.user_data.get("claude_session_id"),
            "project_slug": thread_context["project_slug"],
        }

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        """Return True if path is within root."""
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    @staticmethod
    def _extract_message_thread_id(update: Update) -> Optional[int]:
        """Extract topic/thread id from update message for forum/direct topics."""
        message = update.effective_message
        if not message:
            return None
        message_thread_id = getattr(message, "message_thread_id", None)
        if isinstance(message_thread_id, int) and message_thread_id > 0:
            return message_thread_id
        dm_topic = getattr(message, "direct_messages_topic", None)
        topic_id = getattr(dm_topic, "topic_id", None) if dm_topic else None
        if isinstance(topic_id, int) and topic_id > 0:
            return topic_id
        # Telegram omits message_thread_id for the General topic in forum
        # supergroups; its canonical thread ID is 1.
        chat = update.effective_chat
        if chat and getattr(chat, "is_forum", False):
            return 1
        return None

    async def _reject_for_thread_mode(self, update: Update, message: str) -> None:
        """Send a guidance response when strict thread routing rejects an update."""
        query = update.callback_query
        if query:
            try:
                await query.answer()
            except Exception:
                pass
            if query.message:
                await query.message.reply_text(message, parse_mode="HTML")
            return

        if update.effective_message:
            await update.effective_message.reply_text(message, parse_mode="HTML")

    def register_handlers(self, app: Application) -> None:
        """Register classic handler set."""
        self._register_classic_handlers(app)

    def _register_classic_handlers(self, app: Application) -> None:
        """Register full classic handler set (moved from core.py)."""
        from .handlers import callback, command, message

        handlers = [
            ("start", command.start_command),
            ("new", command.new_session),
            ("projects", command.show_projects),
            ("status", command.session_status),
            ("git", command.git_command),
            ("restart", command.restart_command),
            ("mode", self.mode_command),
        ]
        if self.settings.enable_project_threads:
            handlers.append(("sync_threads", command.sync_threads))

        for cmd, handler in handlers:
            app.add_handler(CommandHandler(cmd, self._inject_deps(handler)))

        app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._inject_deps(message.handle_text_message),
            ),
            group=10,
        )
        app.add_handler(
            MessageHandler(
                filters.Document.ALL, self._inject_deps(message.handle_document)
            ),
            group=10,
        )
        app.add_handler(
            MessageHandler(filters.PHOTO, self._inject_deps(message.handle_photo)),
            group=10,
        )
        app.add_handler(
            MessageHandler(filters.VOICE, self._inject_deps(message.handle_voice)),
            group=10,
        )
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._permission_callback),
                pattern=r"^tool_approval:",
            )
        )
        app.add_handler(
            CallbackQueryHandler(self._inject_deps(callback.handle_callback_query))
        )

        logger.info("Classic handlers registered (15 commands + full handler set)")

    async def get_bot_commands(self) -> list:  # type: ignore[type-arg]
        """Return bot commands for the Telegram command menu."""
        commands = [
            BotCommand("start", "Start bot and show help"),
            BotCommand("new", "Clear context and start fresh session"),
            BotCommand("projects", "Show all projects"),
            BotCommand("status", "Show session status"),
            BotCommand("git", "Git repository commands"),
            BotCommand("restart", "Restart the bot"),
            BotCommand("mode", "Set permission mode (ask/auto/yolo/plan)"),
        ]
        if self.settings.enable_project_threads:
            commands.append(BotCommand("sync_threads", "Sync project topics"))
        return commands

    async def _permission_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle real-time tool approval callbacks (``tool_approval:`` pattern).

        Resolves a pending :class:`ToolApprovalManager` request so that the
        blocked ``can_use_tool`` coroutine can resume with the user's decision.
        """
        query = update.callback_query
        await query.answer()

        # callback_data format: "tool_approval:{approval_id}:{decision}"
        parts = (query.data or "").split(":", 2)
        if len(parts) != 3:
            await query.edit_message_text("⚠️ Malformed approval callback.")
            return
        _, approval_id, decision = parts

        manager: Optional[ToolApprovalManager] = context.user_data.get(
            "_approval_manager"
        )
        if manager is None:
            await query.edit_message_text(
                "⚠️ No active approval session — this request may have expired."
            )
            return

        manager.resolve(approval_id, decision)

        _labels: Dict[str, str] = {
            "allow": "✅ Allowed",
            "deny": "❌ Denied",
            "allow_all": "✅✅ Allowed All",
        }
        label = _labels.get(decision, decision)
        original_text = query.message.text if query.message else ""
        try:
            await query.edit_message_text(
                f"{original_text}\n\n→ {label}",
                parse_mode="HTML",
                reply_markup=None,
            )
        except Exception:
            pass

    # Mode alias -> SDK permission_mode value
    _MODE_MAP: Dict[str, str] = {
        "ask": "default",
        "auto": "acceptEdits",
        "yolo": "bypassPermissions",
        "plan": "plan",
    }

    # Display info per alias
    _MODE_INFO: Dict[str, Dict[str, str]] = {
        "ask": {
            "label": "Ask",
            "emoji": "\U0001f512",
            "desc": "Claude will ask permission before each action.",
        },
        "auto": {
            "label": "Auto",
            "emoji": "\u2705",
            "desc": "Claude will auto-approve file edits, ask for others.",
        },
        "yolo": {
            "label": "YOLO",
            "emoji": "\U0001f525",
            "desc": "Claude will auto-approve everything.",
        },
        "plan": {
            "label": "Plan",
            "emoji": "\U0001f4cb",
            "desc": "Claude will plan actions and ask for approval before executing.",
        },
    }

    def _get_current_mode_alias(self, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Return the friendly alias for the current permission mode."""
        pm = context.user_data.get("permission_mode")
        if pm is None:
            return "ask"
        # Reverse lookup
        for alias, sdk_val in self._MODE_MAP.items():
            if sdk_val == pm:
                return alias
        return "ask"

    async def mode_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Show or set the Claude Code permission mode."""
        args = update.message.text.split()[1:] if update.message.text else []

        if not args:
            # Show current mode
            alias = self._get_current_mode_alias(context)
            info = self._MODE_INFO[alias]
            default_tag = " (default)" if alias == "ask" else ""
            text = (
                f"{info['emoji']} Current mode: <b>{info['label']}</b>{default_tag}\n"
                f"{info['desc']}\n\n"
                "Available modes:\n"
                "\u2022 /mode ask \u2014 Ask before each action (default)\n"
                "\u2022 /mode auto \u2014 Auto-approve file edits\n"
                "\u2022 /mode yolo \u2014 Auto-approve everything\n"
                "\u2022 /mode plan \u2014 Plan only, approve before execute"
            )
            await update.message.reply_text(text, parse_mode="HTML")
            return

        alias = args[0].lower()
        if alias not in self._MODE_MAP:
            await update.message.reply_text(
                "Unknown mode. Use: /mode ask | auto | yolo | plan"
            )
            return

        sdk_mode = self._MODE_MAP[alias]
        info = self._MODE_INFO[alias]

        # Store SDK permission mode (None means SDK default = "default")
        context.user_data["permission_mode"] = sdk_mode

        # Sync plan_mode flag for compatibility with /plan and Approve/Reject buttons
        context.user_data["plan_mode"] = alias == "plan"

        default_tag = " (default)" if alias == "ask" else ""
        await update.message.reply_text(
            f"{info['emoji']} Mode set to <b>{info['label']}</b>{default_tag}\n"
            f"{info['desc']}",
            parse_mode="HTML",
        )
