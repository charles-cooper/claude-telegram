# Claude Code Telegram Notifications

Get Telegram notifications when Claude Code needs your attention.

## Features

- **Permission prompts**: Notified when Claude asks to run Bash commands, edit files, etc.
- **Compaction**: Notified when Claude compacts context

For permission prompts, notifications include full context:
- Bash: command + description
- Edit: unified diff of changes
- Write: file path

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
        "hooks": [
          {
            "type": "command",
            "command": "python3 /path/to/telegram-hook.py"
          }
        ]
      }
    ],
    "PreCompact": [
      {
        "matcher": "auto",
        "hooks": [
          {
            "type": "command",
            "command": "python3 /path/to/telegram-hook.py"
          }
        ]
      },
      {
        "matcher": "manual",
        "hooks": [
          {
            "type": "command",
            "command": "python3 /path/to/telegram-hook.py"
          }
        ]
      }
    ]
  }
}
```

## Notification types

| Matcher | Triggers when |
|---------|---------------|
| `permission_prompt` | Claude needs permission for a tool |
| `PreCompact` | Claude compacts context (both auto and manual) |

## Files

| File | Purpose |
|------|---------|
| `~/telegram.json` | Bot token and chat ID |
| `~/.claude/settings.json` | Claude Code hooks config |
| `/tmp/claude-telegram-state.json` | Message state for reply tracking |
| `/tmp/claude-telegram-hook.log` | Debug log |
