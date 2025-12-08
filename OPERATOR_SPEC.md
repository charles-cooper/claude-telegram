# Claude Army - Multi-Instance Task Management

## Overview

Manage multiple Claude instances, each working on a separate task/feature/PR in isolated git worktrees. An Operator Claude interprets user instructions and manages Worker Claudes.

## Architecture

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

## Core Concepts

### Entities

| Entity | Description | Lifetime |
|--------|-------------|----------|
| **Task** | Unit of work (feature, bug fix, PR) | Until cleanup |
| **Worktree** | Git worktree in `repo/trees/<name>/` | Tied to task |
| **Worker Claude** | Claude instance in worktree | Ephemeral, resurrected |
| **Topic** | Telegram forum topic for task | Tied to task |
| **Operator Claude** | Management Claude in ~/claude-army | Always running |

### Source of Truth Hierarchy

1. **Worktrees** - Define what tasks exist (marker file `.claude-army-task`)
2. **Pinned messages** - Store task metadata (recoverable from Telegram)
3. **Registry** - Cache at `~/claude-army/operator/registry.json` (rebuildable)
4. **Sessions** - Ephemeral, resurrected as needed

## Directory Structure

```
~/claude-army/                    # Operator Claude's home
  telegram-daemon.py
  operator/                       # Gitignored - operator state
    registry.json                 # Cache (rebuildable)
    config.json                   # Group ID, topic IDs, etc.
  ...

~/projects/myrepo/                # User's repository
  trees/
    feature-x/                    # Worktree for task
      .claude-army-task           # Marker file with metadata
      ...
    fix-bug-123/
      .claude-army-task
      ...
```

### Marker File Format

```json
// .claude-army-task
{
  "task_name": "feature-x",
  "repo": "/home/user/myrepo",
  "description": "Add dark mode support",
  "topic_id": 123456,
  "created_at": "2025-01-01T00:00:00Z",
  "status": "active"
}
```

### Post-Worktree Setup Hook

If a repo contains `.claude-army-setup.sh` (in repo root), it runs after worktree creation:

```bash
#!/bin/bash
# .claude-army-setup.sh - runs in new worktree directory
# Example: create symlinks, copy .env, etc.

ln -sf ~/shared/.env .env
ln -sf ../main/node_modules node_modules
```

The script receives environment variables:
- `TASK_NAME` - name of the task
- `REPO_PATH` - path to main repo
- `WORKTREE_PATH` - path to new worktree

### Registry Cache Format

```json
// ~/.claude-army/registry.json
{
  "group_id": -100123456789,
  "general_topic_id": 1,
  "repos": {
    "/home/user/myrepo": {
      "worktree_base": "trees",
      "tasks": ["feature-x", "fix-bug-123"]
    }
  },
  "operator_pane": "operator:0.0"
}
```

## Telegram Setup

### Initial Setup Flow

1. User creates Telegram group, adds bot
2. User sends `/setup` in group
3. Bot checks: not already configured elsewhere
4. Bot converts group to Forum (supergroup with topics)
5. Bot creates "General" topic for Operator Claude
6. Bot stores `group_id` in config
7. Daemon starts Operator Claude session

### Multi-Group Protection

```
/setup in Group A → Success
/setup in Group B → "Already configured for Group A. Run /reset first."
```

### Topic Structure

- **General** - Operator Claude, management commands, fallback notifications
- **task-name** - One per task, Worker Claude notifications

### Pinned Message Metadata

Each task topic has a pinned message:
```json
{
  "task": "feature-x",
  "repo": "/home/user/myrepo",
  "branch": "feature-x",
  "worktree": "trees/feature-x",
  "description": "Add dark mode support",
  "created_at": "2025-01-01T00:00:00Z",
  "status": "active"
}
```

## Operator Claude

### Role

- Runs in `~/claude-army` directory
- Receives all messages from General topic
- Interprets user intent (natural language)
- Manages tasks (spawn, status, cleanup)
- **Manages todo queue** - receives todos from any topic, decides routing/priority
- **Updates AGENTS.md** - observes repeated difficulties across workers, updates project AGENTS.md with learnings
- **Spawn assistance** - learns from previous spawns, asks clarifying questions if task seems ambiguous, enriches initial prompts with context from past worker struggles
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

User: "Clean up the refactor-api task"
Operator: → Permission prompt: "Delete worktree and close topic?"
  → User approves
Operator: "Cleaned up refactor-api task."
```

### Available Actions

| Action | Triggers | Permission Required |
|--------|----------|---------------------|
| Spawn task | User request | Yes (creates worktree) |
| List tasks | User request | No |
| Task status | User request | No |
| Pause task | User request | No |
| Resume task | User request | No |
| Cleanup task | User request | Yes (deletes worktree) |
| Discover repo | User mentions repo | No |

## Worker Claude Sessions

### Lifecycle

```
Spawn:
  1. Create git worktree from master
  2. Create marker file with metadata
  3. Create Telegram topic
  4. Pin metadata message in topic
  5. Create tmux session
  6. Start Claude with task description

Running:
  - Notifications → task topic
  - User replies → Worker Claude
  - Permission prompts → task topic buttons

Death (crash, reboot):
  - Daemon detects missing session
  - Resurrects: `claude --resume` in worktree
  - Topic continues working

Pause:
  - Mark status="paused" in marker file
  - Kill session (won't resurrect)

Cleanup:
  - Kill session
  - Delete worktree
  - Rename topic to "[DONE] task-name"
```

### tmux Session Naming

```
Operator: claude-operator
Workers: claude-{repo_name}-{task_name}
```

### Claude Startup

- New task: `claude "Add dark mode support"`
- Resume after death: `claude --resume`

## Daemon Changes

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

### Session Resurrection

```python
def check_and_resurrect():
    for repo in registry["repos"]:
        for task_name in repo["tasks"]:
            worktree = get_worktree_path(repo, task_name)
            marker = load_marker_file(worktree)

            if marker["status"] == "paused":
                continue

            session_name = f"claude-{repo_name}-{task_name}"
            if not tmux_session_exists(session_name):
                resurrect_session(worktree, session_name)
                log(f"Resurrected session for {task_name}")
```

### Message Routing to Operator

```python
def handle_general_topic_message(message):
    if is_command(message):  # /setup, /reset
        handle_command(message)
    else:
        # Forward to Operator Claude
        send_to_operator_pane(message.text)
```

## Commands

### Bot Commands (any topic)

| Command | Description |
|---------|-------------|
| `/setup` | Initialize group as Claude Army control center |
| `/reset` | Remove configuration (allows setup elsewhere) |
| `/help` | Show available commands |
| `/todo <item>` | Add todo to Operator's queue (from any topic) |
| `/debug` | Debug a notification (reply to it) |

### Natural Language (via Operator Claude)

- "Create a task to fix bug #123 in vyper"
- "What's the status of all tasks?"
- "Pause the feature-x task"
- "Clean up completed tasks"
- "List all repos"

**Routing rules:**
- `/todo` and `/debug` always route to the Operator, even from task topics
- When user replies to a message, the replied-to message (with Telegram metadata like msg_id, topic, timestamp) is included as context
- The Operator manages the todo queue and decides which worker (if any) should handle each item

## Registry Recovery

If `~/.claude-army/registry.json` is corrupted/lost:

1. Scan for marker files: `find ~ -name ".claude-army-task"`
2. For each marker file:
   - Read task metadata
   - Verify topic exists (Telegram API)
   - Add to registry
3. Rebuild repo list from discovered tasks

## Implementation Phases

### Phase 1: Foundation
- [ ] Telegram Forum setup (`/setup` command)
- [ ] Topic creation API integration
- [ ] Pinned message metadata storage
- [ ] Registry cache implementation
- [ ] Marker file format and creation

### Phase 2: Operator Claude
- [ ] Operator tmux session management
- [ ] Message routing to Operator pane
- [ ] Operator response capture and send to Telegram
- [ ] Basic spawn/list/status/cleanup tools

### Phase 3: Worker Management
- [ ] Worktree creation/deletion
- [ ] Worker session lifecycle (spawn, death detection, resurrection)
- [ ] Notification routing by worktree
- [ ] Pause/resume functionality

### Phase 4: Robustness
- [ ] Registry recovery from marker files
- [ ] Topic metadata recovery
- [ ] Multi-group protection
- [ ] Error handling and retries

### Phase 5: Polish
- [ ] Natural language command interpretation
- [ ] Auto-adopt manual worktrees
- [ ] Cleanup after PR merge
- [ ] Status indicators in topic names
