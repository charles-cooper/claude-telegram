# Claude Army

Manage an army of Claude instances.

Get Telegram notifications when Claude Code needs your attention, and respond directly from Telegram.

## Features

- **Permission prompts**: Notified when Claude asks to run Bash commands, edit files, etc.
- **Interactive buttons**: Allow or Deny permission requests directly from Telegram
- **Text replies**: Reply to any notification to send input to Claude
- **Compaction**: Notified when Claude starts and completes context compaction

For permission prompts, notifications include full context:
- Bash: command + description
- Edit: unified diff of changes
- Write: file path + content
- Read: file path
- AskUserQuestion: questions with options

This tool is in BETA mode and under active development. There may be weird behavior or edge cases! If you would like to try it out and/or contribute, please run the daemon inside of a claude instance in your claude-army/ directory, and you can ask it to help you debug things!

Debug flow: reply to a message with `/debug` to get its debug info, then forward that to claude-army instance and ask it to debug it (including what you expected vs what happened).

## Requirements

- Claude Code running inside a tmux session (the daemon uses tmux to inject keystrokes)
- Python 3 with `requests` library
- A Telegram bot (see Installation)

## Installation

### Quick install

```bash
./install.sh
```

The install script will:
1. Install the `requests` Python package if missing
2. Prompt for your Telegram bot token and chat ID
3. Save credentials to `~/telegram.json`
4. Add hooks to `~/.claude/settings.json` (merges with existing settings)

To uninstall:

```bash
./uninstall.sh
```

### Manual install

1. Install dependencies:

```bash
pip3 install requests
```

2. Create `~/telegram.json` with your bot credentials:

```json
{
  "bot_token": "123456:ABC-DEF...",
  "chat_id": "123456789"
}
```

To get these:
- **bot_token**: Message @BotFather on Telegram, send `/newbot`
- **chat_id**: Message your bot, then visit `https://api.telegram.org/bot<TOKEN>/getUpdates`

3. Add hooks to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "Notification": [
      {
        "matcher": "permission_prompt",
        "hooks": [{"type": "command", "command": "python3 /path/to/telegram-hook.py"}]
      }
    ],
    "PreCompact": [
      {"matcher": "auto", "hooks": [{"type": "command", "command": "python3 /path/to/telegram-hook.py"}]},
      {"matcher": "manual", "hooks": [{"type": "command", "command": "python3 /path/to/telegram-hook.py"}]}
    ],
    "PostCompact": [
      {"matcher": "auto", "hooks": [{"type": "command", "command": "python3 /path/to/telegram-hook.py"}]},
      {"matcher": "manual", "hooks": [{"type": "command", "command": "python3 /path/to/telegram-hook.py"}]}
    ]
  }
}
```

## Running the Daemon

To respond to notifications from Telegram, run the daemon:

```bash
./telegram-daemon.py
```

The daemon:
- Polls Telegram for button clicks and text replies
- Sends responses to Claude via tmux keystrokes
- Requires Claude Code to be running in tmux

For background operation:

```bash
nohup ./telegram-daemon.py > /tmp/telegram-daemon.log 2>&1 &
```

## Usage

### Permission Prompts

When Claude needs permission, you'll receive a notification with:
- The assistant's message explaining what it wants to do
- Details of the tool call (command, diff, file contents, etc.)
- **Allow** and **Deny** buttons

Click **Allow** to approve, or **Deny** to reject with optional instructions.

### Text Replies

Reply to any notification to send text to Claude:
- If there's a pending permission prompt and you reply to it, your text becomes the rejection reason
- If there's no pending prompt, your text is sent as regular user input
- If you reply to a different message while a permission is pending, it's blocked (to avoid corrupting TUI state)

### Compaction Notifications

You'll be notified when:
- Context compaction starts (PreCompact)
- Context compaction completes (PostCompact)

### Bot Commands

| Command | Handler | Description |
|---------|---------|-------------|
| `/setup` | Daemon | Initialize a Telegram group as the control center |
| `/status` | Daemon | Show all active tasks and their status |
| `/spawn <description>` | Operator | Create a new task |
| `/cleanup [task]` | Operator | Clean up a task (kill session, remove worktree if applicable) |
| `/show-tmux-command` | Daemon | Show tmux attach command for the current topic's session |
| `/dump` | Daemon | Dump recent tmux pane output for the current topic |
| `/todo <item>` | Daemon | Add todo to TODO.local.md in task directory |
| `/debug` | Daemon | Debug a message (reply to the message first) |
| `/rebuild-registry` | Daemon | Rebuild task registry from marker files (maintenance) |
| `/summarize` | Operator | Summarize status of all tasks |
| `/help` | Daemon | Show available commands |

**Handler types:**
- **Daemon**: Handled programmatically by the telegram daemon
- **Operator**: Routed to Operator Claude for interpretation and execution

## Notification types

| Event | Triggers when |
|-------|---------------|
| `permission_prompt` | Claude needs permission for a tool |
| `PreCompact` | Claude starts compacting context |
| `PostCompact` | Claude finishes compacting context |

## Architecture

Two components work together:

1. **telegram-hook.py** - Hook script invoked by Claude Code on events, sends notifications to Telegram
2. **telegram-daemon.py** - Long-running daemon that polls Telegram and injects responses via tmux

## Files

| File | Purpose |
|------|---------|
| `~/telegram.json` | Bot token and chat ID |
| `~/.claude/settings.json` | Claude Code hooks config |
| `/tmp/claude-telegram-state.json` | Message state for reply tracking |
| `/tmp/claude-telegram-state.lock` | File lock for state |
| `/tmp/claude-telegram-hook.log` | Debug log |
