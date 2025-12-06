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
  "hook_event_name": "Notification" | "PreCompact" | "PostCompact",
  "notification_type": "permission_prompt" | null,
  "trigger": "auto" | "manual",  // PreCompact/PostCompact only
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
| `PostCompact` | "Context compaction complete (auto\|manual)" | None |

### Tool Formatting

| Tool | Format |
|------|--------|
| Bash | Code block with command + description |
| Edit | Unified diff |
| Write | File path + content in code block |
| Read | File path |
| AskUserQuestion | Questions with options |
| Other | JSON of input |

### Markdown Escaping

All text content is escaped to prevent Telegram markdown parsing errors:
- `_`, `*`, `` ` ``, `[`, `]` are escaped with backslash
- Triple backticks inside code blocks replaced with `'''`
- Assistant text prefix is escaped
- Description text preserves underscores for intentional italics

### State File
Writes to `/tmp/claude-telegram-state.json`:
```json
{
  "message_id": {
    "session_id": "uuid",
    "cwd": "/path",
    "pane": "session:window.pane",
    "type": "permission_prompt",
    "transcript_path": "/path/to/transcript.jsonl",
    "tool_use_id": "toolu_..."
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

| Action | Condition | Behavior |
|--------|-----------|----------|
| Allow | `data="y"` + `type="permission_prompt"` | Accept the permission prompt |
| Deny | `data="n"` + `type="permission_prompt"` | Select "Tell Claude something else" (empty) |
| Text reply | Reply to any message | See text reply logic below |

### Text Reply Logic
1. Find pane and transcript_path from replied-to message
2. Check transcript directly for any pending tool_use (more reliable than state)
3. If pending tool exists:
   - If replying to THAT tool's message → use permission input (Down Down + text)
   - Else → block and reply on Telegram: "⚠️ Ignored: there's a pending permission prompt"
4. If no pending tool → send as regular input

**Future options for blocked case:**
- Option 2: Send anyway as regular input (may not work if prompt blocking)
- Option 3: Queue message, send after permission handled

### Button Updates
After action, update button via `POST /bot{token}/editMessageReplyMarkup`:
- Allow → "Allowed"
- Deny → "Reply with instructions"
- Text reply (permission only) → "Replied"
- Stale → "Expired"

After handling Allow/Deny/Stale, message is marked as handled to prevent re-sending navigation keys. Handled permission prompts still accept text replies as regular user input.

### Stale Detection
A message is stale if a newer message exists for the same tmux pane.

### Cleanup
Every 5 minutes:
- Remove state entries for dead tmux panes
- Mark TUI-handled permission prompts by checking transcript for tool_result

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

### Permission Prompt Options
The permission dialog has three options:
1. **Yes** - Accept and run the tool
2. **Yes, and don't ask again** - Accept and add to allow list
3. **Tell Claude something else** - Reject with custom instructions

### Injecting Input via tmux
Use `tmux send-keys` to inject keystrokes. Arrow keys navigate menus, text goes to input fields.

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
    ],
    "PostCompact": [
      {"matcher": "auto", "hooks": [{"type": "command", "command": "..."}]},
      {"matcher": "manual", "hooks": [{"type": "command", "command": "..."}]}
    ]
  }
}
```
