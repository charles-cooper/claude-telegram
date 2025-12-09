# Agent Instructions

## Mandatory: Read After Compaction

Always read this file after context compaction to restore critical codebase knowledge.

## Mandatory: Update SPEC.md

After ANY change to functionality, update `SPEC.md` to reflect the change. No exceptions. The spec documents all API contracts, data formats, and behaviors for maintaining the code when upstream APIs change.

## Running the Daemon

**IMPORTANT**: You own the daemon as a background task. Use `run_in_background: true` with the Bash tool to start `./telegram-daemon.py`. Never use shell background syntax (`&`) as it prevents output monitoring. When you need to restart the daemon, use KillShell on the background bash ID, then start a new background task.

## Codebase Overview

This is a Telegram integration for Claude Code that watches transcripts and sends notifications:

**Main components:**
- `telegram-daemon.py` - Main daemon, orchestrates everything
- `transcript_watcher.py` - Watches transcript files for tool_use and compaction events
- `telegram_poller.py` - Handles Telegram callbacks and messages
- `telegram_utils.py` - Shared utilities (formatting, state, API)

## Code Style

- **No inline imports**: Always import at top level unless absolutely necessary to break circular dependencies.

## Design Principles

- **100% Resource Correctness**: Never accept resource leakage, orphans, or zombies as acceptable. Every resource created (Telegram topics, tmux sessions, marker files) must be properly tracked and cleaned up. If an operation can fail partway through, design it so partial state is recoverable or rolled back.

## Critical Learnings

### Claude Code TUI Navigation
- Permission prompts use **arrow keys**, NOT text input (y/n doesn't work)
- Default selection is "Yes" - just press Enter to allow
- "Tell Claude something else" is the 3rd option - navigate with Down Down Enter

### tmux send-keys
- Text and Enter MUST be separate commands: `send-keys "text"` then `send-keys Enter`
- Single command `send-keys "text" Enter` buffers text but doesn't submit
- Clear input buffer with Ctrl-U before sending text (but not before arrow keys)

### Telegram API
- Use `inline_keyboard` with `callback_data` for buttons
- `editMessageReplyMarkup` to update buttons after action
- `answerCallbackQuery` to dismiss loading state on button click
- Android client has limited syntax highlighting (no green for diff + lines)
- `getUpdates` timeout parameter must be **integer** (seconds) - floats like 0.5 don't work for long polling

### Hook Events
- `permission_prompt` - contains generic message, read transcript for actual tool details
- `PreCompact` - triggered before context compaction (auto or manual)
- `elicitation_dialog` is MCP-only, not for built-in tools

### State Management
- State in `/tmp/claude-telegram-state.json` with file locking (fcntl)
- Track pane per message for multi-session support
- Check for stale prompts by comparing message IDs per pane
- PID file at `/tmp/claude-telegram-daemon.pid` - no need to clean up stale files, check_singleton verifies process is running

## Testing

To test permission flow:
1. Start daemon: `./telegram-daemon.py`
2. Trigger a permission prompt (edit, bash without auto-approve)
3. Check Telegram for notification with Allow/Deny buttons
4. Click button, verify action in Claude TUI

Debug logs: `/tmp/claude-telegram-hook.log`

Check state: `jq . /tmp/claude-telegram-state.json`
Check hooks: `jq .hooks ~/.claude/settings.json`
