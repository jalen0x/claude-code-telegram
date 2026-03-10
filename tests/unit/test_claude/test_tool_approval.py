"""Unit tests for ToolApprovalManager."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.claude.tool_approval import ToolApprovalManager, _format_tool_summary


class TestToolApprovalManager:
    """Tests for ToolApprovalManager."""

    def _make_manager(self) -> ToolApprovalManager:
        bot = AsyncMock()
        bot.send_message = AsyncMock(return_value=MagicMock(message_id=42))
        bot.edit_message_text = AsyncMock()
        return ToolApprovalManager(bot=bot, chat_id=123)

    @pytest.mark.asyncio
    async def test_read_tools_auto_approved(self) -> None:
        mgr = self._make_manager()
        result = await mgr.request_approval("Read", {"file_path": "/tmp/x"})
        assert result == "allow"
        # No Telegram message should be sent for read tools
        mgr._bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_write_tool_sends_approval_request(self) -> None:
        mgr = self._make_manager()

        async def _click_allow():
            await asyncio.sleep(0.05)
            mgr.resolve("0", "allow")

        task = asyncio.create_task(_click_allow())
        result = await mgr.request_approval("Edit", {"file_path": "src/main.py"})
        await task

        assert result == "allow"
        mgr._bot.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_deny_blocks_tool(self) -> None:
        mgr = self._make_manager()

        async def _click_deny():
            await asyncio.sleep(0.05)
            mgr.resolve("0", "deny")

        task = asyncio.create_task(_click_deny())
        result = await mgr.request_approval("Bash", {"command": "rm -rf /"})
        await task

        assert result == "deny"

    @pytest.mark.asyncio
    async def test_allow_all_auto_approves_subsequent(self) -> None:
        mgr = self._make_manager()

        # First call: user clicks "allow_all"
        async def _click_allow_all():
            await asyncio.sleep(0.05)
            mgr.resolve("0", "allow_all")

        task = asyncio.create_task(_click_allow_all())
        result = await mgr.request_approval("Edit", {"file_path": "a.py"})
        await task
        assert result == "allow_all"

        # Second call: should auto-approve without sending a message
        mgr._bot.send_message.reset_mock()
        result2 = await mgr.request_approval("Bash", {"command": "echo hi"})
        assert result2 == "allow"
        mgr._bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolve_unknown_id_is_noop(self) -> None:
        mgr = self._make_manager()
        # Should not raise
        mgr.resolve("nonexistent", "allow")

    @pytest.mark.asyncio
    async def test_cleanup_denies_pending(self) -> None:
        mgr = self._make_manager()

        async def _cleanup_soon():
            await asyncio.sleep(0.05)
            await mgr.cleanup()

        task = asyncio.create_task(_cleanup_soon())
        result = await mgr.request_approval("Write", {"file_path": "x.py"})
        await task

        assert result == "deny"

    @pytest.mark.asyncio
    async def test_send_failure_returns_deny(self) -> None:
        mgr = self._make_manager()
        mgr._bot.send_message.side_effect = RuntimeError("Telegram down")
        result = await mgr.request_approval("Edit", {"file_path": "x.py"})
        assert result == "deny"

    def test_is_write_tool(self) -> None:
        mgr = self._make_manager()
        assert mgr.is_write_tool("Edit") is True
        assert mgr.is_write_tool("Bash") is True
        assert mgr.is_write_tool("Read") is False
        assert mgr.is_write_tool("Grep") is False

    @pytest.mark.asyncio
    async def test_counter_increments(self) -> None:
        mgr = self._make_manager()

        async def _resolve_immediately(approval_id: str):
            await asyncio.sleep(0.05)
            mgr.resolve(approval_id, "allow")

        task0 = asyncio.create_task(_resolve_immediately("0"))
        await mgr.request_approval("Edit", {"file_path": "a.py"})
        await task0

        task1 = asyncio.create_task(_resolve_immediately("1"))
        await mgr.request_approval("Write", {"file_path": "b.py"})
        await task1

        assert mgr._counter == 2


class TestFormatToolSummary:
    """Tests for _format_tool_summary helper."""

    def test_edit_shows_file_path(self) -> None:
        result = _format_tool_summary("Edit", {"file_path": "src/main.py"})
        assert "src/main.py" in result

    def test_bash_shows_command(self) -> None:
        result = _format_tool_summary("Bash", {"command": "echo hello"})
        assert "echo hello" in result

    def test_bash_truncates_long_command(self) -> None:
        long_cmd = "x" * 200
        result = _format_tool_summary("Bash", {"command": long_cmd})
        assert "..." in result
        assert len(result) < 300

    def test_notebook_shows_path(self) -> None:
        result = _format_tool_summary(
            "NotebookEdit", {"notebook_path": "analysis.ipynb"}
        )
        assert "analysis.ipynb" in result

    def test_unknown_tool(self) -> None:
        result = _format_tool_summary("CustomTool", {"foo": "bar"})
        assert "CustomTool" in result
