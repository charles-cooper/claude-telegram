# Claude Code Telegram Integration - Feature Plan

## Overview
Telegram bot for Claude Code notifications and remote control.

---

## Completed

### Smarter Message Deletion
- Notifications track `notified_at` timestamp
- Quick response (< 4s) → delete (false positive)
- Slow response (>= 4s) → mark expired (TUI-handled)

### Idle Notifications
- Text-only assistant messages trigger idle notifications immediately
- If tool_use follows within 4s, notification is deleted (supersession)
- Otherwise stays visible for remote user

---

## 1. TODO: Claude Operator / Orchestration (Future)

### Vision
A Claude orchestration system controlled via Telegram:
- Spawn multiple Claude instances, each in its own tmux session
- Tear down instances when done
- Control them remotely from Telegram
- Eventually: a "manager" Claude that coordinates worker instances

### Telegram Commands

| Command | Action |
|---------|--------|
| `/spawn <project> [prompt]` | Create new tmux session, start Claude in project dir, optionally send initial prompt |
| `/list` | List active Claude sessions with status |
| `/kill <session>` | Send Ctrl+C, then kill tmux session |
| `/send <session> <text>` | Send text to specific session |
| `/status <session>` | Show recent transcript activity |
| `/todo <session> <item>` | Add item to Claude's internal todo stack |

### Future: Manager Claude

A supervisory Claude instance that:
1. Receives high-level tasks from user
2. Spawns worker Claudes for subtasks
3. Monitors their progress via transcripts
4. Handles permission prompts on their behalf (or escalates to human)
5. Aggregates results
