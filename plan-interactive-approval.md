# Interactive Tool Approval — Implementation Plan

## Overview

Replace the current "plan mode" (re-run after approval) with real-time interactive tool approval via the SDK's `can_use_tool` callback. When Claude wants to execute a write operation, the bot pauses, sends an approval request to Telegram with inline buttons, waits for the user's decision, then continues or aborts.

This matches how Claude Code CLI works: read operations auto-proceed, write operations require confirmation.

---

## Architecture

```
User sends message
       │
       ▼
orchestrator.py / message.py
       │
       ▼
facade.py run_command(permission_mode, bot, chat_id, user_data)
       │
       ▼
sdk_integration.py execute_command()
       │
       ├─ Creates ClaudeSDKClient with can_use_tool callback
       │
       ▼
Claude Code CLI process
       │
       ├─ Read tool (Read/Grep/Glob/LS) → can_use_tool → auto Allow ✅
       │
       ├─ Write tool (Write/Edit/Bash) → can_use_tool → 
       │       │
       │       ├─ Send Telegram message: "Claude wants to Edit src/main.py"
       │       ├─ Show [✅ Allow] [❌ Deny] [✅ Allow All] buttons
       │       ├─ Wait for user click (asyncio.Event, 120s timeout)
       │       ├─ User clicks → set Event result → return Allow/Deny
       │       └─ Timeout → return Deny
       │
       └─ Result → back to Telegram
```

---

## Key SDK Facts (verified from source)

1. **`can_use_tool` is async** — SDK does `await self.can_use_tool(tool_name, tool_input, context)` at `query.py:259`. We can freely `await` inside it.

2. **Requires streaming mode** — `client.py:113-118` checks that prompt is `AsyncIterable`, not `str`. Current code already uses `connect(None)` + `query(prompt)` pattern (streaming mode). ✅

3. **`set_permission_mode()`** — Available on `ClaudeSDKClient` at `client.py:234`. Sends control request to CLI process. Can upgrade to `bypassPermissions` mid-session for "Allow All".

4. **`PermissionResultAllow`** — Can include `updated_input` (modify tool input) and `updated_permissions` (grant permanent rules). We only need basic Allow/Deny.

5. **One callback at a time** — SDK sends one `can_use_tool` request, waits for response, then continues. No concurrent approval requests for the same client.

---

## Detailed Design

### 1. ToolApprovalManager (new class)

**File: `src/claude/tool_approval.py`** (new file)

```python
import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import structlog
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

logger = structlog.get_logger()

# Tools that require approval in ask/plan mode
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "Bash", "Task", "NotebookEdit"}
READ_TOOLS = {"Read", "Grep", "Glob", "LS", "Skill", "TodoRead", "WebFetch", "WebSearch"}


@dataclass
class PendingApproval:
    """A single pending tool approval request."""
    request_id: str
    tool_name: str
    tool_input: Dict[str, Any]
    event: asyncio.Event = field(default_factory=asyncio.Event)
    result: Optional[str] = None  # "allow", "deny", "allow_all"
    message_id: Optional[int] = None  # Telegram message ID for cleanup


class ToolApprovalManager:
    """Manages interactive tool approval via Telegram inline buttons."""
    
    def __init__(self, bot: Bot, chat_id: int, timeout: float = 120.0):
        self.bot = bot
        self.chat_id = chat_id
        self.timeout = timeout
        self.pending: Dict[str, PendingApproval] = {}
        self.allow_all: bool = False  # Set True when user clicks "Allow All"
    
    async def request_approval(
        self, tool_name: str, tool_input: Dict[str, Any]
    ) -> str:
        """Send approval request to Telegram, wait for user response.
        
        Returns: "allow", "deny", or "allow_all"
        """
        # If user previously clicked "Allow All", auto-approve
        if self.allow_all:
            return "allow"
        
        # Auto-approve read-only tools
        if tool_name in READ_TOOLS:
            return "allow"
        
        request_id = str(uuid.uuid4())[:8]
        pending = PendingApproval(
            request_id=request_id,
            tool_name=tool_name,
            tool_input=tool_input,
        )
        self.pending[request_id] = pending
        
        # Format tool details for display
        detail = self._format_tool_detail(tool_name, tool_input)
        
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Allow", callback_data=f"perm:{request_id}:allow"),
                InlineKeyboardButton("❌ Deny", callback_data=f"perm:{request_id}:deny"),
            ],
            [
                InlineKeyboardButton("✅ Allow All (this session)", callback_data=f"perm:{request_id}:allow_all"),
            ],
        ])
        
        msg = await self.bot.send_message(
            chat_id=self.chat_id,
            text=f"🔐 <b>Permission Request</b>\n\n"
                 f"Claude wants to use <b>{tool_name}</b>:\n"
                 f"<code>{detail}</code>\n\n"
                 f"⏱️ Auto-deny in {int(self.timeout)}s",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        pending.message_id = msg.message_id
        
        # Wait for user response or timeout
        try:
            await asyncio.wait_for(pending.event.wait(), timeout=self.timeout)
        except asyncio.TimeoutError:
            pending.result = "deny"
            # Edit message to show timeout
            try:
                await self.bot.edit_message_text(
                    chat_id=self.chat_id,
                    message_id=msg.message_id,
                    text=f"🔐 <b>Permission Request</b> — ⏰ <i>Timed out (denied)</i>\n\n"
                         f"Claude wanted to use <b>{tool_name}</b>:\n"
                         f"<code>{detail}</code>",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        finally:
            self.pending.pop(request_id, None)
        
        result = pending.result or "deny"
        
        if result == "allow_all":
            self.allow_all = True
            result = "allow"
        
        return result
    
    def resolve(self, request_id: str, decision: str) -> bool:
        """Called by CallbackQueryHandler when user clicks a button.
        
        Returns True if request was found and resolved.
        """
        pending = self.pending.get(request_id)
        if not pending:
            return False
        
        pending.result = decision
        pending.event.set()
        return True
    
    def _format_tool_detail(self, tool_name: str, tool_input: Dict[str, Any]) -> str:
        """Format tool input for readable display."""
        if tool_name in ("Write", "Edit", "MultiEdit"):
            path = tool_input.get("file_path") or tool_input.get("path", "?")
            return f"{path}"
        elif tool_name == "Bash":
            cmd = tool_input.get("command", "?")
            if len(cmd) > 200:
                cmd = cmd[:200] + "..."
            return cmd
        elif tool_name == "Task":
            desc = tool_input.get("description", "?")
            if len(desc) > 200:
                desc = desc[:200] + "..."
            return desc
        else:
            return str(tool_input)[:200]
```

### 2. Wire ToolApprovalManager into can_use_tool

**File: `src/claude/sdk_integration.py`**

Modify `_make_can_use_tool_callback` to accept an optional `ToolApprovalManager`:

```python
def _make_can_use_tool_callback(
    security_validator: SecurityValidator,
    working_directory: Path,
    approved_directory: Path,
    approval_manager: Optional[ToolApprovalManager] = None,  # NEW
) -> Any:
    async def can_use_tool(tool_name, tool_input, context):
        # 1. Existing security boundary checks (unchanged)
        if tool_name in _FILE_TOOLS:
            file_path = tool_input.get("file_path") or tool_input.get("path")
            if file_path:
                if _is_claude_internal_path(file_path):
                    return PermissionResultAllow()
                valid, _resolved, error = security_validator.validate_path(
                    file_path, working_directory
                )
                if not valid:
                    return PermissionResultDeny(message=error or "Invalid file path")
        
        if tool_name in _BASH_TOOLS:
            command = tool_input.get("command", "")
            if command:
                valid, error = check_bash_directory_boundary(
                    command, working_directory, approved_directory
                )
                if not valid:
                    return PermissionResultDeny(message=error or "Bash boundary violation")
        
        # 2. NEW: Interactive approval for write tools
        if approval_manager and tool_name in WRITE_TOOLS:
            decision = await approval_manager.request_approval(tool_name, tool_input)
            if decision == "allow":
                return PermissionResultAllow()
            else:
                return PermissionResultDeny(message="User denied this action")
        
        # 3. Default: allow (for read tools or when no approval_manager)
        return PermissionResultAllow()
    
    return can_use_tool
```

### 3. Thread approval_manager through the call chain

**File: `src/claude/sdk_integration.py` — `execute_command()`**

Add `approval_manager` parameter:

```python
async def execute_command(
    self,
    prompt: str,
    working_directory: Path,
    session_id: Optional[str] = None,
    continue_session: bool = False,
    stream_callback: Optional[Callable[[StreamUpdate], None]] = None,
    permission_mode: Optional[Literal["default", "acceptEdits", "plan", "bypassPermissions"]] = None,
    approval_manager: Optional[ToolApprovalManager] = None,  # NEW
) -> ClaudeResponse:
```

Pass it to `_make_can_use_tool_callback`:

```python
if self.security_validator:
    options.can_use_tool = _make_can_use_tool_callback(
        security_validator=self.security_validator,
        working_directory=working_directory,
        approved_directory=self.config.approved_directory,
        approval_manager=approval_manager,  # NEW
    )
```

**File: `src/claude/facade.py` — `run_command()` and `_execute()`**

Thread `approval_manager` through both methods:

```python
async def run_command(
    self, ...,
    approval_manager: Optional[ToolApprovalManager] = None,  # NEW
) -> ClaudeResponse:

async def _execute(
    self, ...,
    approval_manager: Optional[ToolApprovalManager] = None,  # NEW
) -> ClaudeResponse:
```

### 4. Create ToolApprovalManager in orchestrator/message handler

**File: `src/bot/orchestrator.py` — `agentic_text()` (~L1050)**

Before calling `run_command`:

```python
from src.claude.tool_approval import ToolApprovalManager

# Create approval manager only for ask/plan modes
approval_manager = None
if permission_mode in (None, "default", "plan"):
    approval_manager = ToolApprovalManager(
        bot=context.bot,
        chat_id=chat.id,
        timeout=120.0,
    )

claude_response = await claude_integration.run_command(
    ...,
    permission_mode=permission_mode,
    approval_manager=approval_manager,  # NEW
)
```

**File: `src/bot/handlers/message.py` — `handle_text_message()` (~L390)**

Same pattern — create `ToolApprovalManager` when in ask/plan mode.

### 5. Register CallbackQueryHandler for permission buttons

**File: `src/bot/orchestrator.py` — `_register_agentic_handlers()`**

Add a new `CallbackQueryHandler` for `perm:` pattern:

```python
# Permission approval callbacks
app.add_handler(
    CallbackQueryHandler(
        self._inject_deps(self._permission_callback),
        pattern=r"^perm:",
    )
)
```

**File: `src/bot/orchestrator.py` — `_register_classic_handlers()`**

Same handler registration, before the general callback handler.

### 6. Implement _permission_callback

**File: `src/bot/orchestrator.py`**

```python
async def _permission_callback(
    self, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle permission approval/denial button clicks."""
    query = update.callback_query
    await query.answer()
    
    # Parse: "perm:{request_id}:{decision}"
    parts = query.data.split(":")
    if len(parts) != 3:
        return
    
    _, request_id, decision = parts
    
    # Find the ToolApprovalManager — stored in user_data during execution
    approval_manager = context.user_data.get("_approval_manager")
    if not approval_manager:
        await query.edit_message_text("⚠️ No active approval session.")
        return
    
    resolved = approval_manager.resolve(request_id, decision)
    if not resolved:
        await query.edit_message_text("⚠️ This approval request has expired.")
        return
    
    # Update the message to show decision
    emoji = {"allow": "✅", "deny": "❌", "allow_all": "✅✅"}
    label = {"allow": "Allowed", "deny": "Denied", "allow_all": "Allowed All"}
    
    try:
        old_text = query.message.text or ""
        await query.edit_message_text(
            text=f"{old_text}\n\n{emoji.get(decision, '❓')} <b>{label.get(decision, decision)}</b>",
            parse_mode="HTML",
        )
    except Exception:
        pass
    
    # If "Allow All", also update user_data permission_mode
    if decision == "allow_all":
        context.user_data["permission_mode"] = "bypassPermissions"
```

### 7. Store approval_manager in user_data

In both `agentic_text()` and `handle_text_message()`, store the manager so the callback handler can find it:

```python
if approval_manager:
    context.user_data["_approval_manager"] = approval_manager

# ... run_command() ...

# Clean up after execution
context.user_data.pop("_approval_manager", None)
```

### 8. Remove old plan mode Approve/Reject buttons

The old plan mode approach (re-run after approval) is replaced by real-time approval. Remove:

- `_plan_callback()` method
- `plan:approve` / `plan:reject` CallbackQueryHandler registration
- `pending_plan_prompt` storage in user_data
- Plan keyboard construction after response
- Same cleanup in `message.py`

The `/plan` command now just sets `permission_mode = "plan"`, which the SDK handles natively (Claude only plans, doesn't execute).

### 9. Mode behavior matrix

| Mode | Read tools | Write tools | Behavior |
|------|-----------|-------------|----------|
| `ask` (default/None) | Auto-allow | Interactive approval | Telegram buttons per tool |
| `auto` (acceptEdits) | Auto-allow | Auto-allow edits, ask Bash | SDK handles natively |
| `yolo` (bypassPermissions) | Auto-allow | Auto-allow | No approval needed |
| `plan` | Auto-allow | Interactive approval | Same as ask but Claude plans first |

Note: For `auto` (acceptEdits), the SDK itself handles edit auto-approval. We only need the `ToolApprovalManager` for `ask` and `plan` modes.

---

## Files Changed Summary

| File | Change |
|------|--------|
| `src/claude/tool_approval.py` | **NEW** — ToolApprovalManager class |
| `src/claude/sdk_integration.py` | Add `approval_manager` param, wire into `can_use_tool` |
| `src/claude/facade.py` | Thread `approval_manager` through `run_command` / `_execute` |
| `src/bot/orchestrator.py` | Create manager in `agentic_text`, add `_permission_callback`, register handler, remove old plan buttons |
| `src/bot/handlers/message.py` | Create manager in `handle_text_message`, remove old plan buttons |
| `tests/unit/test_orchestrator.py` | Update callback counts, remove plan button tests, add permission tests |
| `tests/unit/test_claude/test_tool_approval.py` | **NEW** — Unit tests for ToolApprovalManager |

## Implementation Order

1. `src/claude/tool_approval.py` — standalone, no dependencies
2. `src/claude/sdk_integration.py` — add approval_manager param
3. `src/claude/facade.py` — thread param through
4. `src/bot/orchestrator.py` — create manager, register callback, remove old plan buttons
5. `src/bot/handlers/message.py` — same for classic mode
6. Tests
7. Lint + test

## Edge Cases

1. **User clicks button after timeout** — `resolve()` returns False, callback shows "expired" message
2. **Bot restart during approval wait** — `asyncio.Event` is lost, SDK gets no response, eventually times out on the SDK side too
3. **Multiple tools in sequence** — Each gets its own approval request, one at a time (SDK is sequential)
4. **"Allow All" mid-session** — Sets `allow_all=True` on manager, all subsequent tools auto-approved
5. **User sends new message during approval** — Should be blocked or queued (existing behavior — one request at a time per user)
