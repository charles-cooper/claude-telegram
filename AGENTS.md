# Agent Instructions

## Mandatory: Update SPEC.md

After ANY change to functionality, update `SPEC.md` to reflect the change. No exceptions. The spec documents all API contracts, data formats, and behaviors for maintaining the code when upstream APIs change.

## Codebase Overview

This is a Telegram integration for Claude Code that:
1. `telegram-hook.py` - Hook that sends notifications to Telegram when Claude needs input
2. `telegram-daemon.py` - Daemon that polls Telegram and injects replies into Claude via tmux

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

### Hook Events
- `permission_prompt` - contains generic message, read transcript for actual tool details
- `PreCompact` - triggered before context compaction (auto or manual)
- `elicitation_dialog` is MCP-only, not for built-in tools

### State Management
- State in `/tmp/claude-telegram-state.json` with file locking (fcntl)
- Track pane per message for multi-session support
- Check for stale prompts by comparing message IDs per pane

## Testing

To test permission flow:
1. Start daemon: `./telegram-daemon.py`
2. Trigger a permission prompt (edit, bash without auto-approve)
3. Check Telegram for notification with Allow/Deny buttons
4. Click button, verify action in Claude TUI

Debug logs: `/tmp/claude-telegram-hook.log`

Check state: `jq . /tmp/claude-telegram-state.json`
Check hooks: `jq .hooks ~/.claude/settings.json`
