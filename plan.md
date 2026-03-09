# Plan Mode Feature — Revised Implementation Plan

## Summary

Add a `/plan` toggle command to the Telegram bot that switches Claude Code's `permission_mode` to `"plan"`. When active, Claude will plan actions and require approval before executing them. The toggle is per-user, stored in `context.user_data`.

## Design Decisions

### Re-run approach (not `set_permission_mode()`)
The SDK has `client.set_permission_mode()` for dynamic switching, but our architecture creates a **new `ClaudeSDKClient` per `execute_command()` call** (see `sdk_integration.py:249`). There is no long-lived client to call `set_permission_mode()` on. Instead, we pass `permission_mode` through the options when constructing each client. This is safer and consistent with existing architecture.

### Classic mode: agentic-only with clear error
Plan mode is agentic-only. Classic mode's `handle_text_message` (`message.py:296`) doesn't use the same `run_command` flow and would require significant refactoring. Instead, `/plan` in classic mode returns a clear error message: "Plan mode is only available in agentic mode."

---

## Changes by File

### 1. `pyproject.toml` — Fix SDK version mismatch (Finding #2)

**Line 50:** Change `claude-agent-sdk = "^0.1.39"` → `claude-agent-sdk = "^0.1.48"`

This unifies the declared dependency with what's actually installed in `.venv` (v0.1.48) and ensures `permission_mode` on `ClaudeAgentOptions` is available.

---

### 2. `src/claude/sdk_integration.py` — Accept `permission_mode` parameter

**`execute_command()` (L149-156):** Add `permission_mode: Optional[str] = None` parameter.

```python
async def execute_command(
    self,
    prompt: str,
    working_directory: Path,
    session_id: Optional[str] = None,
    continue_session: bool = False,
    stream_callback: Optional[Callable[[StreamUpdate], None]] = None,
    permission_mode: Optional[str] = None,  # NEW
) -> ClaudeResponse:
```

**`ClaudeAgentOptions` construction (L198-215):** Add `permission_mode` to the options dict if provided:

After line 213 (`setting_sources=["project"]`), add:
```python
permission_mode=permission_mode,
```

The SDK's `ClaudeAgentOptions` already accepts `permission_mode: Literal['default', 'acceptEdits', 'plan', 'bypassPermissions'] | None = None`, so no SDK changes needed.

---

### 3. `src/claude/facade.py` — Thread `permission_mode` through facade

**`run_command()` (L32-40):** Add `permission_mode: Optional[str] = None` parameter.

```python
async def run_command(
    self,
    prompt: str,
    working_directory: Path,
    user_id: int,
    session_id: Optional[str] = None,
    on_stream: Optional[Callable[[StreamUpdate], None]] = None,
    force_new: bool = False,
    permission_mode: Optional[str] = None,  # NEW
) -> ClaudeResponse:
```

**`_execute()` (L148-163):** Add `permission_mode: Optional[str] = None` parameter, pass it through to `sdk_manager.execute_command()`.

```python
async def _execute(
    self,
    prompt: str,
    working_directory: Path,
    session_id: Optional[str] = None,
    continue_session: bool = False,
    stream_callback: Optional[Callable] = None,
    permission_mode: Optional[str] = None,  # NEW
) -> ClaudeResponse:
    return await self.sdk_manager.execute_command(
        prompt=prompt,
        working_directory=working_directory,
        session_id=session_id,
        continue_session=continue_session,
        stream_callback=stream_callback,
        permission_mode=permission_mode,  # NEW
    )
```

Both `_execute()` calls in `run_command()` (L82 and L106) must pass `permission_mode=permission_mode`.

---

### 4. `src/bot/orchestrator.py` — Add `/plan` command, callback handler, wire into `agentic_text`

#### 4a. Add `/plan` command handler

Add new method `agentic_plan` (after `agentic_verbose`, around L540):

```python
async def agentic_plan(
    self, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Toggle plan mode on/off."""
    plan_active = context.user_data.get("plan_mode", False)

    if update.message.text and len(update.message.text.split()) > 1:
        arg = update.message.text.split()[1].lower()
        if arg in ("on", "1", "true"):
            plan_active = True
        elif arg in ("off", "0", "false"):
            plan_active = False
        else:
            await update.message.reply_text(
                "Usage: /plan [on|off]\nCurrently: "
                + ("on" if plan_active else "off")
            )
            return
        context.user_data["plan_mode"] = plan_active
    else:
        # Toggle
        plan_active = not plan_active
        context.user_data["plan_mode"] = plan_active

    status = "ON" if plan_active else "OFF"
    description = (
        " — Claude will plan actions and ask for approval before executing."
        if plan_active
        else " — Claude will execute actions directly."
    )
    await update.message.reply_text(f"Plan mode: <b>{status}</b>{description}", parse_mode="HTML")
```

#### 4b. Register `/plan` command in `_register_agentic_handlers()` (L299-316)

Add to the `handlers` list at L304-311:
```python
("plan", self.agentic_plan),
```

#### 4c. Add `/plan` to `get_bot_commands()` agentic section (L413-423)

Add to the commands list:
```python
BotCommand("plan", "Toggle plan mode (approve before execute)"),
```

#### 4d. Add plan approval callback handler — SEPARATE from `_agentic_callback`

Add a **new** `CallbackQueryHandler` with `pattern=r'^plan:'` in `_register_agentic_handlers()`, after the existing `cd:` handler (L347-353):

```python
# Plan mode approval/rejection callbacks
app.add_handler(
    CallbackQueryHandler(
        self._inject_deps(self._plan_callback),
        pattern=r"^plan:",
    )
)
```

This is a **separate handler** from `_agentic_callback` (which handles `cd:` only). Finding #1 explicitly called out that stuffing into the existing handler would cause routing conflicts.

#### 4e. Add `_plan_callback` method

New method to handle `plan:approve` and `plan:reject` callbacks:

```python
async def _plan_callback(
    self, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle plan approval/rejection callbacks."""
    query = update.callback_query
    await query.answer()

    data = query.data  # "plan:approve" or "plan:reject"
    action = data.split(":", 1)[1]

    if action == "approve":
        # Re-run the pending prompt with permission_mode=None (execute normally)
        pending = context.user_data.pop("pending_plan_prompt", None)
        if not pending:
            await query.edit_message_text("No pending plan to approve.")
            return

        await query.edit_message_text("Approved! Executing plan...")

        # Re-run through agentic_text flow with plan mode temporarily off
        # by passing permission_mode="acceptEdits" for execution
        claude_integration = context.bot_data.get("claude_integration")
        if not claude_integration:
            await query.message.reply_text("Claude integration not available.")
            return

        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        session_id = context.user_data.get("claude_session_id")

        chat = query.message.chat
        progress_msg = await chat.send_message("Executing approved plan...")
        heartbeat = self._start_typing_heartbeat(chat)

        try:
            claude_response = await claude_integration.run_command(
                prompt=pending,
                working_directory=current_dir,
                user_id=query.from_user.id,
                session_id=session_id,
                permission_mode="acceptEdits",
            )
            context.user_data["claude_session_id"] = claude_response.session_id

            from .utils.formatting import ResponseFormatter
            formatter = ResponseFormatter(self.settings)
            formatted = formatter.format_claude_response(claude_response.content)

            await progress_msg.delete()
            for msg in formatted:
                await chat.send_message(
                    msg.text, parse_mode=msg.parse_mode
                )
        except Exception as e:
            logger.error("Plan execution failed", error=str(e))
            await progress_msg.edit_text(f"Execution failed: {e}")
        finally:
            heartbeat.cancel()

    elif action == "reject":
        context.user_data.pop("pending_plan_prompt", None)
        await query.edit_message_text("Plan rejected. Send a new message to try again.")
```

#### 4f. Wire `permission_mode` into `agentic_text()` (L937)

At L937, the `claude_integration.run_command()` call needs to pass `permission_mode`:

```python
# Determine permission mode
permission_mode = "plan" if context.user_data.get("plan_mode") else None

claude_response = await claude_integration.run_command(
    prompt=message_text,
    working_directory=current_dir,
    user_id=user_id,
    session_id=session_id,
    on_stream=on_stream,
    force_new=force_new,
    permission_mode=permission_mode,  # NEW
)
```

#### 4g. After receiving plan response, show approval buttons

After the `run_command` call in `agentic_text`, when plan mode is active and the response is a plan (not an error), show inline approval buttons:

```python
if context.user_data.get("plan_mode") and not claude_response.is_error:
    # Store prompt for re-execution on approval
    context.user_data["pending_plan_prompt"] = message_text

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Approve", callback_data="plan:approve"),
            InlineKeyboardButton("Reject", callback_data="plan:reject"),
        ]
    ])
    # Send plan with approval buttons
    # (formatted_messages already built above)
    # Add keyboard to the last message
```

This integrates into the existing response-sending flow at ~L998-1060.

---

### 5. Classic mode: reject `/plan` with clear message (Finding #4)

#### `_register_classic_handlers()` (L357-408)

Add a `/plan` command handler that returns a clear error:

```python
("plan", self._classic_plan_unsupported),
```

Add method:
```python
async def _classic_plan_unsupported(
    self, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Reject /plan in classic mode."""
    await update.message.reply_text(
        "Plan mode is only available in agentic mode.\n"
        "Set AGENTIC_MODE=true to enable it."
    )
```

Also add to `get_bot_commands()` classic section (L425-443) so it appears in the command menu with a note:
```python
BotCommand("plan", "Toggle plan mode (agentic mode only)"),
```

---

### 6. `tests/unit/test_orchestrator.py` — Update existing + add new tests (Finding #5)

#### 6a. Fix broken assertion at L154

**Current (L152-155):**
```python
# 4 message handlers (text, document, photo, voice)
assert len(msg_handlers) == 4
# 1 callback handler (for cd: only)
assert len(cb_handlers) == 1
```

**Updated:**
```python
# 4 message handlers (text, document, photo, voice)
assert len(msg_handlers) == 4
# 2 callback handlers (cd: for project selection, plan: for plan approval)
assert len(cb_handlers) == 2
```

#### 6b. Fix command count at L103

**Current (L85-109):** Asserts 6 commands.

**Updated:** Assert **7** commands (add `plan`):
```python
assert len(cmd_handlers) == 7
# ... add:
assert frozenset({"plan"}) in commands
```

#### 6c. Fix `get_bot_commands` count at L163

**Current (L158-165):** Asserts 6 bot commands.

**Updated:** Assert **7** bot commands:
```python
assert len(commands) == 7
cmd_names = [c.command for c in commands]
assert "plan" in cmd_names
```

#### 6d. Fix classic command count at L128

**Current:** Asserts 14 commands.

**Updated:** Assert **15** commands (adding `/plan` rejection handler).

#### 6e. Fix classic bot commands count at L173

**Current:** Asserts 14 bot commands.

**Updated:** Assert **15**.

#### 6f. Update `test_agentic_callback_scoped_to_cd_pattern` (L325-344)

This test asserts only 1 callback handler. Update to expect 2 and verify both patterns:

```python
async def test_agentic_callbacks_cd_and_plan_patterns(agentic_settings, deps):
    """Agentic callback handlers are registered with cd: and plan: patterns."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)
    app = MagicMock()
    app.add_handler = MagicMock()
    orchestrator.register_handlers(app)

    from telegram.ext import CallbackQueryHandler
    cb_handlers = [
        call[0][0]
        for call in app.add_handler.call_args_list
        if isinstance(call[0][0], CallbackQueryHandler)
    ]

    assert len(cb_handlers) == 2
    patterns = {h.pattern.pattern for h in cb_handlers}
    assert r"^cd:" in patterns
    assert r"^plan:" in patterns
```

#### 6g. New tests to add

```python
async def test_agentic_plan_toggle(agentic_settings, deps):
    """Plan command toggles plan_mode in user_data."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)
    update = MagicMock()
    update.message.text = "/plan"
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.user_data = {}

    # First toggle: off -> on
    await orchestrator.agentic_plan(update, context)
    assert context.user_data["plan_mode"] is True

    # Second toggle: on -> off
    await orchestrator.agentic_plan(update, context)
    assert context.user_data["plan_mode"] is False


async def test_agentic_plan_explicit_on_off(agentic_settings, deps):
    """Plan command accepts on/off arguments."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.user_data = {}

    update.message.text = "/plan on"
    await orchestrator.agentic_plan(update, context)
    assert context.user_data["plan_mode"] is True

    update.message.text = "/plan off"
    await orchestrator.agentic_plan(update, context)
    assert context.user_data["plan_mode"] is False


async def test_classic_plan_returns_error(classic_settings, deps):
    """Classic mode /plan returns unsupported message."""
    orchestrator = MessageOrchestrator(classic_settings, deps)
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.user_data = {}
    context.bot_data = {}
    for k, v in deps.items():
        context.bot_data[k] = v

    await orchestrator._classic_plan_unsupported(update, context)

    call_args = update.message.reply_text.call_args
    assert "agentic mode" in call_args.args[0].lower()


async def test_agentic_text_passes_plan_permission_mode(agentic_settings, deps):
    """When plan_mode is active, run_command receives permission_mode='plan'."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)

    mock_response = MagicMock()
    mock_response.session_id = "session-plan"
    mock_response.content = "Here is my plan..."
    mock_response.tools_used = []
    mock_response.is_error = False

    claude_integration = AsyncMock()
    claude_integration.run_command = AsyncMock(return_value=mock_response)

    update = MagicMock()
    update.effective_user.id = 123
    update.message.text = "Refactor the auth module"
    update.message.message_id = 1
    update.message.chat.send_action = AsyncMock()
    update.message.chat.type = "group"
    update.message.reply_text = AsyncMock()
    update.message.message_thread_id = None

    progress_msg = AsyncMock()
    progress_msg.delete = AsyncMock()
    update.message.reply_text.return_value = progress_msg

    context = MagicMock()
    context.user_data = {"plan_mode": True}
    context.bot_data = {
        "settings": agentic_settings,
        "claude_integration": claude_integration,
        "storage": None,
        "rate_limiter": None,
        "audit_logger": None,
    }

    await orchestrator.agentic_text(update, context)

    call_kwargs = claude_integration.run_command.call_args
    assert call_kwargs.kwargs.get("permission_mode") == "plan"
```

---

## Implementation Order

1. **pyproject.toml** — Bump `claude-agent-sdk` to `^0.1.48`
2. **sdk_integration.py** — Add `permission_mode` param, pass to `ClaudeAgentOptions`
3. **facade.py** — Thread `permission_mode` through `run_command()` and `_execute()`
4. **orchestrator.py** — Add `agentic_plan`, `_plan_callback`, `_classic_plan_unsupported`, register handlers, wire `permission_mode` into `agentic_text`
5. **test_orchestrator.py** — Update counts, update pattern test, add new tests
6. Run `make lint` and `make test` to verify

## Codex Review Findings Addressed

| # | Finding | Resolution |
|---|---------|------------|
| 1 | Callback handler routing conflict | Separate `CallbackQueryHandler(pattern=r'^plan:')` registered alongside existing `cd:` handler |
| 2 | SDK version mismatch | `pyproject.toml` bumped from `^0.1.39` to `^0.1.48` |
| 3 | `set_permission_mode()` alternative | Documented why re-run approach is used (no long-lived client); kept re-run |
| 4 | Classic mode needs handling | `/plan` in classic mode returns clear error; agentic-only feature |
| 5 | Tests need updating | 6 specific test assertions updated + 4 new tests added |
