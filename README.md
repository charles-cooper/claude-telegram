# Claude Army

Manage an army of Claude instances.

Get Telegram notifications when Claude Code needs your attention, and respond directly from Telegram.

This is supposed to augment a tmux / git worktree based workflow (not replace it). It is expected that you may occasionally want to drop into a tmux session to debug or check on things!

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

To get bot credentials:
- **bot_token**: Message @BotFather on Telegram, send `/newbot`
- **chat_id**: Message your bot, then visit `https://api.telegram.org/bot<TOKEN>/getUpdates`

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
| `/dump` | Daemon | Dump recent tmux pane output for the current topic |
| `/debug` | Daemon | Debug a message (reply to the message first) |
| `/show-tmux-command` | Daemon | Show tmux attach command for the current topic's session |
| `/spawn <description>` | Operator | Create a new task |
| `/status` | Daemon | Quick list of tasks (instant, from registry) |
| `/cleanup [task]` | Operator | Clean up a task (kill session, remove worktree if applicable) |
| `/help` | Daemon | Show available commands |
| `/todo <item>` | Daemon | Add todo to TODO.local.md in task directory |
| `/setup` | Daemon | Initialize a Telegram group as the control center |
| `/summarize` | Operator | Analyze tasks with TODOs, prioritize, suggest next steps |
| `/operator [msg]` | Operator | Request operator intervention for current task |
| `/rebuild-registry` | Daemon | Rebuild task registry from marker files (maintenance) |

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

The system uses a daemon-based architecture:

**telegram-daemon.py** - Long-running daemon that:
- Polls Claude Code transcript files for events (permission prompts, context compaction, etc.)
- Polls Telegram API for button clicks and text replies
- Sends notifications to Telegram with appropriate routing (topics for multi-instance setups)
- Injects responses back to Claude via tmux keystrokes
- Handles bot commands and task management

## Files

| File | Purpose |
|------|---------|
| `~/telegram.json` | Bot token and chat ID configuration |
| `/tmp/claude-telegram-state.json` | Message state for reply tracking |
| `/tmp/claude-telegram-daemon.pid` | PID file for daemon singleton check |
| `/tmp/task-registry.json` | Registry of active Claude tasks/sessions |
