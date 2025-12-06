# Telegram Notifier Specification

## Overview

Two components work together:
1. **telegram-hook.py** - Hook invoked by Claude Code on events, sends notifications to Telegram
2. **telegram-daemon.py** - Long-running daemon that polls Telegram and injects responses into Claude via tmux

## telegram-hook.py

### Purpose
Receives events from Claude Code hooks, extracts context, sends formatted Telegram messages.

### Input (stdin)
JSON object from Claude Code hook system:

```json
{
  "hook_event_name": "Notification" | "PreCompact",
  "notification_type": "permission_prompt" | null,
  "trigger": "auto" | "manual",  // PreCompact only
  "message": "...",
  "cwd": "/path/to/project",
  "session_id": "uuid",
  "transcript_path": "/path/to/transcript.jsonl"
}
```

### Transcript Format
JSONL file, each line:
```json
{
  "type": "assistant" | "user" | "tool_result",
  "message": {
    "content": [
      {"type": "text", "text": "..."},
      {"type": "tool_use", "name": "Bash", "input": {...}}
    ]
  }
}
```

### Output Behavior

| Event | Message Format | Buttons |
|-------|----------------|---------|
| `permission_prompt` | Assistant text + formatted tool call | Allow / Deny |
| `PreCompact` | "Compacting context (auto\|manual)..." | None |

### Tool Formatting

| Tool | Format |
|------|--------|
| Bash | Code block with command + description |
| Edit | Unified diff |
| Write | File path |
| Read | File path |
| AskUserQuestion | Questions with options |
| Other | JSON of input |

### State File
Writes to `/tmp/claude-telegram-state.json`:
```json
{
  "message_id": {
    "session_id": "uuid",
    "cwd": "/path",
    "pane": "session:window.pane",
    "type": "permission_prompt"  // only for permission prompts
  }
}
```

### Config File
Reads `~/telegram.json`:
```json
{
  "bot_token": "123456:ABC...",
  "chat_id": "123456789"
}
```

### Telegram API Used
- `POST /bot{token}/sendMessage`
  - `chat_id`, `text`, `parse_mode: "Markdown"`
  - `reply_markup.inline_keyboard` for buttons

---

## telegram-daemon.py

### Purpose
Polls Telegram for button clicks and text replies, sends responses to Claude via tmux.

### Telegram Polling
- `GET /bot{token}/getUpdates?offset={n}&timeout=30`
- Long polling with 30s timeout
- Track offset to avoid reprocessing

### Update Types

#### Callback Query (button click)
```json
{
  "callback_query": {
    "id": "...",
    "data": "y" | "n" | "_",
    "message": {"message_id": 123, "chat": {"id": 456}}
  }
}
```

#### Message (text reply)
```json
{
  "message": {
    "message_id": 124,
    "chat": {"id": 456},
    "text": "user input",
    "reply_to_message": {"message_id": 123}
  }
}
```

### Response Handling

| Action | Condition | tmux Commands |
|--------|-----------|---------------|
| Allow | `data="y"` + `type="permission_prompt"` | `send-keys Enter` |
| Deny | `data="n"` + `type="permission_prompt"` | `send-keys Down`, `Down`, `Enter` |
| Text reply | Any reply to tracked message | `send-keys C-u`, `{text}`, `Enter` |
| Ignore y/n | `data="y"\|"n"` + no permission_prompt | Answer callback only |

### Button Updates
After action, update button via `POST /bot{token}/editMessageReplyMarkup`:
- Allow → "Allowed"
- Deny → "Reply with instructions"
- Text reply (permission only) → "Replied"
- Stale → "Expired"

### Stale Detection
A message is stale if a newer message exists for the same tmux pane.

### Cleanup
Every 5 minutes, remove state entries for dead tmux panes.

### Telegram API Used
- `GET /bot{token}/getUpdates` - poll for updates
- `POST /bot{token}/answerCallbackQuery` - dismiss button loading
- `POST /bot{token}/editMessageReplyMarkup` - update button label

### tmux Commands
- `tmux has-session -t {pane}` - check pane exists
- `tmux send-keys -t {pane} {key}` - send keystrokes
- `tmux display-message -p "#{session_name}:#{window_index}.#{pane_index}"` - get pane ID

---

## Claude Code TUI Behavior

### Permission Prompt Navigation
The permission dialog uses **arrow key navigation**, not text input:
- Option 1: "Yes" (default, selected)
- Option 2: "Yes, and don't ask again"
- Option 3: "Tell Claude something else"

To allow: `Enter`
To deny: `Down` `Down` `Enter`

### tmux send-keys Quirk
Text and Enter must be separate commands:
```bash
# Correct
tmux send-keys -t pane "text"
tmux send-keys -t pane Enter

# Wrong (buffers but doesn't submit)
tmux send-keys -t pane "text" Enter
```

### Input Buffer
Clear with `C-u` before sending text (not needed before arrow keys).

---

## File Locations

| File | Purpose |
|------|---------|
| `~/telegram.json` | Bot credentials |
| `~/.claude/settings.json` | Hook configuration |
| `/tmp/claude-telegram-state.json` | Message tracking |
| `/tmp/claude-telegram-state.lock` | File lock |
| `/tmp/claude-telegram-hook.log` | Debug log |

---

## Hook Configuration

In `~/.claude/settings.json`:
```json
{
  "hooks": {
    "Notification": [{"matcher": "permission_prompt", "hooks": [{"type": "command", "command": "..."}]}],
    "PreCompact": [
      {"matcher": "auto", "hooks": [{"type": "command", "command": "..."}]},
      {"matcher": "manual", "hooks": [{"type": "command", "command": "..."}]}
    ]
  }
}
```
