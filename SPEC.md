# Claude Army Specification

## Overview

Claude Army is a multi-instance task management system with Telegram integration. It manages multiple Claude instances working on separate tasks in isolated git worktrees, with an Operator Claude interpreting user instructions and managing Worker Claudes.

**Architecture:**
```
┌─────────────────────────────────────────────────────────────┐
│                 Telegram Forum Group                        │
├─────────────────────────────────────────────────────────────┤
│  General Topic              │  Task Topics                  │
│  ────────────────           │  ────────────                 │
│  User ↔ Operator Claude     │  feature-x: Worker Claude A   │
│  Natural language commands  │  fix-bug-123: Worker Claude B │
│  Setup, status, management  │  refactor-api: Worker Claude C│
└──────────────┬──────────────┴───────────────┬───────────────┘
               │                              │
               ▼                              ▼
       ┌───────────────┐            ┌─────────────────┐
       │ Operator      │            │ Worker Sessions │
       │ tmux session  │            │ (per worktree)  │
       │ ~/claude-army │            │ repo/trees/X    │
       └───────────────┘            └─────────────────┘
               │                              │
               └──────────────┬───────────────┘
                              ▼
                    ┌─────────────────┐
                    │     Daemon      │
                    │ - Watches all   │
                    │   transcripts   │
                    │ - Routes to     │
                    │   correct topic │
                    │ - Handles input │
                    └─────────────────┘
```

**Components:**
- `telegram-daemon.py` - Main daemon, orchestrates transcript watching and Telegram polling
- `transcript_watcher.py` - Watches transcript files for tool_use and compaction events
- `telegram_poller.py` - Handles Telegram updates (callbacks, messages)
- `telegram_utils.py` - Shared utilities (formatting, state, API calls)
- `registry.py` - Task registry and configuration management
- `session_operator.py` - Operator Claude session management
- `session_worker.py` - Worker Claude session management
- `bot_commands.py` - Bot command handlers

## Core Concepts

### Task Types

| Type | Directory | Topic | Cleanup |
|------|-----------|-------|---------|
| **Worktree Task** | Created by us (`repo/trees/{name}/`) | Long-lived (PR/branch work) | Delete worktree + close topic |
| **Session** | Any existing directory | Ephemeral (focused work) | Remove marker + close topic (preserve dir) |

Both types:
- Have `.claude/army.json` marker file
- Have a dedicated Telegram topic
- Are recoverable from filesystem scan

### Source of Truth Hierarchy

1. **Marker files** - `.claude/army.json` defines tasks (inside Claude's own state dir)
2. **Registry** - Cache at `~/claude-army/operator/registry.json` (rebuildable from markers)
3. **tmux sessions** - Ephemeral, resurrected as needed

## Directory Structure

```
~/claude-army/                    # Project root (daemon runs here)
  telegram-daemon.py              # Daemon process
  operator/                       # Operator Claude's working directory (gitignored)
    registry.json                 # Cache (rebuildable from .claude/army.json files)
    config.json                   # Group ID, topic IDs, etc.
    .claude/                      # Operator's Claude state (conversations, settings)
  ...

~/projects/myrepo/                # User's repository
  trees/
    feature-x/                    # Worktree for task
      .claude/
        army.json                 # Marker file (inside Claude's state dir)
        ...                       # Claude's conversation state

~/projects/other-project/         # Existing directory (session, not worktree)
  .claude/
    army.json                     # Session marker
```

### Marker File Format

```json
// .claude/army.json (same format for worktree and session)
{
  "name": "feature-x",
  "type": "worktree",             // or "session"
  "repo": "/home/user/myrepo",    // only for worktrees
  "description": "Add dark mode support",
  "topic_id": 123456,
  "created_at": "2025-01-01T00:00:00Z"
}
```

### Registry Cache Format

```json
// operator/registry.json (rebuildable from .claude/army.json scans)
{
  "tasks": {
    "feature-x": {
      "type": "worktree",
      "path": "/home/user/myrepo/trees/feature-x",
      "repo": "/home/user/myrepo",
      "topic_id": 123,
      "status": "active",
      "pane": "ca-feature-x:0.0"
    }
  }
}
```

Note: `group_id`, `general_topic_id`, and `operator_pane` are in `config.json`, not registry.

## Telegram Setup

### Initial Setup Flow

1. User creates Telegram group, adds bot as admin
2. User enables Topics in group settings
3. User sends `/setup` in group
4. Bot stores `group_id` in config
5. Daemon starts Operator Claude session

### Bot Commands

| Command | Description |
|---------|-------------|
| `/setup` | Initialize group as control center |
| `/status` | Show all tasks and status |
| `/spawn <desc>` | Create a new task (routes to operator) |
| `/cleanup` | Clean up current task (routes to operator) |
| `/tmux` | Show tmux attach command for current topic |
| `/show` | Dump tmux pane output for current topic |
| `/help` | Show available commands |
| `/todo <item>` | Add todo to Operator queue |
| `/debug` | Debug a message (reply to it) |
| `/rebuild-registry` | Rebuild registry from marker files (maintenance) |

Commands are registered via `setMyCommands` API at startup.

### Topic Structure

- **General** - Operator Claude, management commands, fallback notifications
- **task-name** - One per task, Worker Claude notifications

## Operator Claude

### Role

- Runs in `~/claude-army/operator` directory
- Receives all messages from General topic
- Interprets user intent (natural language)
- Manages tasks (spawn, status, cleanup)
- Manages todo queue - receives todos from any topic, decides routing/priority
- Goes through permission prompts for actions

### Example Interactions

```
User: "Can you look into the memory leak in vyper?"
Operator: [identifies repo ~/vyper, creates task description]
  → Permission prompt: "Create worktree vyper/trees/fix-memory-leak?"
  → User approves
Operator: "Created task 'fix-memory-leak' in vyper. Worker Claude is investigating."

User: "What's the status of all tasks?"
Operator: [scans worktrees, checks sessions]
Operator: "3 active tasks:
  - vyper/fix-memory-leak: Running, last activity 2m ago
  - myrepo/feature-x: Idle, waiting for input
  - myrepo/refactor-api: Paused"
```

### Available Actions

| Action | Triggers | Permission Required |
|--------|----------|---------------------|
| Spawn worktree task | User request | Yes (creates worktree) |
| Spawn session | User request | Yes (creates topic) |
| List tasks | User request | No |
| Task status | User request | No |
| Pause task | User request | No |
| Resume task | User request | No |
| Cleanup worktree task | User request | Yes (deletes worktree) |
| Cleanup session | User request | Yes (closes topic) |

## Worker Claude Sessions

### Lifecycle

```
Spawn Worktree Task:
  1. Create git worktree from master
  2. Create Telegram topic
  3. Write .claude/army.json marker
  4. Create tmux session
  5. Start Claude with task description

Spawn Session (existing directory):
  1. Verify directory exists
  2. Create Telegram topic
  3. Write .claude/army.json marker
  4. Create tmux session
  5. Start Claude with task description

Auto-register (daemon discovers Claude):
  1. Daemon sees new transcript
  2. Create Telegram topic
  3. Write .claude/army.json marker
  4. Task is now tracked and routable

Running:
  - Notifications → task topic
  - User replies → Worker Claude
  - Permission prompts → task topic buttons

Death (crash, reboot):
  - Daemon detects missing session
  - Resurrects: `claude --continue` in directory
  - Topic continues working

Cleanup (worktree):
  - Kill session
  - Delete worktree (removes directory + .claude/army.json)
  - Close topic

Cleanup (session):
  - Kill session
  - Remove .claude/army.json (preserve directory)
  - Close topic
```

### tmux Session Naming

Short names for easy mobile access:
```
Operator: ca-op
Workers: ca-{task_name}
```

The `ca-` prefix (claude-army) avoids collisions with user sessions.

### Claude Startup

- **Operator**: `claude --continue || claude` (fall back to fresh if no conversation)
- **New worker task**: `claude "<task description>"`
- **Worker resume after death**: `claude --continue || claude "<task description>"`

### Post-Worktree Setup Hook

If a repo contains `.claude-army-setup.sh` (in repo root), it runs after worktree creation:

```bash
#!/bin/bash
# .claude-army-setup.sh - runs in new worktree directory
ln -sf ~/shared/.env .env
ln -sf ../main/node_modules node_modules
```

Environment variables: `TASK_NAME`, `REPO_PATH`, `WORKTREE_PATH`

## Transcript Watching

### Discovery

The daemon discovers transcripts via:
1. State file entries (transcripts from previous notifications)
2. tmux panes (scans `~/.claude/projects/{encoded-cwd}/*.jsonl`)

### Polling

- Reads from last known position (append-only file)
- Checks every ~1 second
- Detects new `tool_use` entries and sends notifications
- Tracks `tool_result` entries to prune notified set (memory management)

### Typing Indicator

Sends Telegram "typing" action only when Claude is actually working (transcript activity).
- Triggers on any new line in transcript (tool_use, thinking, text, etc.)
- Does NOT trigger on message receipt - only on Claude's response activity
- Routed to appropriate topic based on pane/cwd
- Automatically cancelled when message is sent

### Transcript Format

JSONL file, each line:
```json
{
  "type": "assistant" | "user",
  "message": {
    "content": [
      {"type": "text", "text": "..."},
      {"type": "tool_use", "id": "toolu_...", "name": "Bash", "input": {...}}
    ]
  }
}
```

Tool results:
```json
{
  "type": "user",
  "message": {
    "content": [
      {"type": "tool_result", "tool_use_id": "toolu_..."}
    ]
  }
}
```

Compaction events:
```json
{
  "type": "system",
  "subtype": "compact_boundary",
  "content": "Conversation compacted",
  "compactMetadata": {"trigger": "auto", "preTokens": 155723}
}
```

### Skipped Tools

These tools are never notified (always auto-approved):
- `BashOutput`
- `KillShell`
- `AgentOutputTool`
- `TodoWrite`

### Batched Tool Calls

Claude can send multiple tool_use in a single message. TUI shows them one at a time.

Handling:
1. All tool_use from a message are added to `tool_queue` in order
2. Only the first tool without a result is notified
3. When tool_result arrives, the next queued tool is notified

### Batch Denial

Denying one tool in a batch interrupts all queued tools (Claude behavior).
When user denies via Telegram, all other pending permission prompts for the same pane are expired with "❌ Denied via batch denial".

### Idle Detection

Text-only assistant messages (no tool_use) trigger idle notifications immediately.
- Tracked by Claude message ID (`message.id`)
- If tool_use appears for the same message ID within 4 seconds, notification is deleted (false positive)
- If tool_use appears after 4 seconds, notification is marked superseded (kept for reply capability)
- If no tool_use appears, notification stays (Claude is waiting for input)

## Notifications

### Message Format

```
`project-name`

[assistant text if any]

---

Claude is asking permission to run:
```bash
command here
```
_description_
```

### Buttons

Permission prompts get two buttons:
- `Allow` (callback_data: "y")
- `Deny (or reply)` (callback_data: "n")

### Tool Formatting

| Tool | Format |
|------|--------|
| Bash | Code block with command + description |
| Edit | Unified diff |
| Write | File path + content in code block |
| Read | File path |
| AskUserQuestion | Questions with options |
| Other | JSON of input |

### Markdown Handling

All messages use MarkdownV2 for consistency. Escaping approach:
- Text outside code blocks: escape all special chars
- Code blocks: preserve as-is, only replace ``` with ''' inside
- Inline code: preserve as-is

### Notification Routing

```python
def route_notification(pane, notification):
    worktree_path = get_worktree_for_pane(pane)

    if worktree_path and is_managed_worktree(worktree_path):
        task = load_marker_file(worktree_path)
        send_to_topic(task["topic_id"], notification)
    elif is_operator_pane(pane):
        send_to_general_topic(notification)
    else:
        # Fallback: non-managed Claude session
        send_to_general_topic(notification, prefix="[unmanaged]")
```

## Telegram Polling

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

| Action | Condition | tmux Keys |
|--------|-----------|-----------|
| Allow | `data="y"` | Enter |
| Deny | `data="n"` | Down Down Enter |
| Text reply | Reply to permission msg | Down Down + text + Enter |

### Button Updates

After action:
- Allow → "✓ Allowed"
- Deny → "📝 Reply"
- Text reply → "💬 Replied"
- Stale → "⏰ Expired"

### Smart Notification Deletion

Tool notifications track `notified_at` timestamp. When tool_result arrives:
- If < 4 seconds elapsed: delete notification (was auto-approved, false positive)
- If >= 4 seconds elapsed: mark as expired (was TUI-handled, user may want to see it)

### Text Reply Logic

1. Find pane and transcript_path from replied-to message
2. Check transcript for pending tool_use
3. If pending:
   - If replying to THAT tool's message → permission input
   - Else → block: "⚠️ Ignored: pending permission prompt"
4. If no pending → regular input

### Permission Protection

New messages (not replies) are blocked if there's a pending permission on the target pane.
This prevents accidental approval when Enter is sent to a pane with an active prompt.

User sees: "⚠️ There's a pending permission prompt. Reply to that message to respond, or click Allow/Deny."

## State Management

### State File

`/tmp/claude-telegram-state.json`:
```json
{
  "message_id": {
    "pane": "session:window.pane",
    "type": "permission_prompt",
    "transcript_path": "/path/to/transcript.jsonl",
    "tool_use_id": "toolu_...",
    "tool_name": "Bash",
    "cwd": "/path/to/project",
    "notified_at": 1234567890.123
  }
}
```

Idle notifications:
```json
{
  "message_id": {
    "pane": "session:window.pane",
    "type": "idle",
    "claude_msg_id": "msg_01...",
    "cwd": "/path/to/project",
    "transcript_path": "/path/to/transcript.jsonl",
    "notified_at": 1234567890.123
  }
}
```

### Cleanup

Every 5 minutes:
- Remove entries for dead tmux panes
- Remove watchers for dead panes

### Memory Management

- `notified_tools` set pruned when tool_result seen
- Watchers removed when pane dies
- State entries removed when pane dies

## Registry Recovery

If `operator/registry.json` is corrupted/lost:

1. Scan for marker files: `find ~ -name "army.json" -path "*/.claude/*"`
2. For each `.claude/army.json` found:
   - Read task metadata (name, type, topic_id)
   - Add to registry
3. All tasks (worktree and session) are recovered

Use `/rebuild-registry` command to trigger this manually.

## Crash-Safe Topic Creation

Topic creation uses a pending marker pattern to handle daemon crashes.

### Problem

If daemon crashes between creating a Telegram topic and persisting the topic_id, the topic becomes orphaned. The Bot API cannot enumerate existing topics.

### Solution: Pending Marker Pattern

```
1. Write pending marker: {pending_topic_name: "task-foo", pending_since: "..."}
2. Create topic → API returns topic_id
3. Send setup message: "Setup in progress for task-foo..."
4. Complete marker: {name: "task-foo", topic_id: 123, ...}
5. Send completion: "Setup complete"
```

### Data Structures

**Pending marker** (in `.claude/army.json`):
```json
{
  "pending_topic_name": "task-foo",
  "pending_since": "2024-01-01T12:00:00Z"
}
```

**Topic mapping** (in `config.json`):
```json
{
  "topic_mappings": {
    "12345": "task-foo",
    "12346": "task-bar"
  }
}
```

**Persisted offset** (in `config.json`):
```json
{
  "telegram_offset": 123456789
}
```

### Recovery Mechanisms

1. **Automatic via forum_topic_created**: When polling sees a `forum_topic_created` event, store `topic_id → name` mapping in config.

2. **Message from unknown topic**: When a message arrives from a topic_id not in registry:
   - Try stored mapping → complete pending marker
   - Check if message text matches pending marker name → complete
   - Prompt user with list of pending tasks

3. **Offset persistence**: Store `telegram_offset` in config so we don't miss `forum_topic_created` events after restart.

### Crash Scenarios

| Crash Point | State | Recovery |
|-------------|-------|----------|
| Before step 2 | Pending marker, no topic | Clean up marker on next attempt |
| Between 2-3 | Pending marker, topic exists | `forum_topic_created` mapping → auto-recover |
| Between 3-4 | Pending marker, topic + setup msg | Reply to setup msg OR mapping lookup |
| After 4 | Complete marker | Just missing completion msg (cosmetic) |

### Guarantees

- **No duplicate topics**: Pending marker prevents new creation while uncertain
- **No orphaned topics**: Multi-tier recovery ensures we can always link topic_id to marker
- **No data loss**: Offset persistence prevents update replay

## Config Files

| File | Purpose |
|------|---------|
| `~/telegram.json` | Bot credentials (`bot_token`, `chat_id`) |
| `operator/config.json` | Group ID, operator pane |
| `operator/registry.json` | Task cache |
| `/tmp/claude-telegram-state.json` | Message tracking |
| `/tmp/claude-telegram-daemon.pid` | Daemon PID |

## Claude Code TUI Behavior

### Permission Prompt Options

1. **Yes** - Accept and run the tool
2. **Yes, and don't ask again** - Accept and add to allow list
3. **Tell Claude something else** - Reject with custom instructions

### tmux Commands

- `tmux has-session -t {pane}` - check pane exists
- `tmux send-keys -t {pane} -l {text}` - send literal text
- `tmux send-keys -t {pane} {key}` - send keystrokes
- `tmux list-panes -a -F '...'` - discover panes

## Implementation Status

### Phase 1: Foundation ✓
- [x] Telegram Forum setup (`/setup` command)
- [x] Topic creation API integration
- [x] Registry cache implementation
- [x] Config management with auto-reload

### Phase 2: Operator Claude ✓
- [x] Operator tmux session management
- [x] Message routing to Operator pane
- [x] Operator response capture and send to Telegram

### Phase 3: Task Management ✓
- [x] Spawn worktree task (create worktree, topic, marker, session)
- [x] Spawn session (create topic, marker for existing directory)
- [x] Auto-register discovered sessions (daemon writes marker)
- [x] Notification routing by task (lookup in registry)
- [x] Cleanup (worktree vs session behavior)
- [x] Permission warning when bot lacks Manage Topics rights

### Phase 4: Session Lifecycle ✓
- [x] Worker session resurrection on death
- [x] Pause/resume functionality
- [ ] Status indicators in topic names (stub exists, not implemented)

### Phase 5: Recovery & Polish
- [x] Registry recovery from `.claude/army.json` scans (`/recover`)
- [x] Natural language command interpretation (via Operator)
- [ ] Cleanup after PR merge (not automated)
