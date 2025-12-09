# Claude Army Test Checklist

## Prerequisites
- [ ] Telegram bot created with BotFather
- [ ] Bot token in `~/telegram.json` (`{"bot_token": "...", "chat_id": "..."}`)
- [ ] Telegram Forum group created (Topics enabled)
- [ ] Bot added to group as admin

## Phase 1: Setup
- [ ] `/setup` in non-forum group → error message about enabling topics
- [ ] `/setup` in forum group → success, stores group_id
- [ ] `/setup` again → "Already set up"
- [ ] `/help` → shows all commands with configured status
- [ ] `/status` → "No active tasks"
- [ ] `/reset` → clears configuration
- [ ] `/setup` again → works

## Phase 2: Operator
- [ ] After `/setup`, tmux session `claude-operator` exists
- [ ] Send message in General topic → routed to operator pane (typing indicator)
- [ ] Operator responds → appears in General topic
- [ ] `/todo fix the bug` → sent to operator with [TODO] prefix
- [ ] Reply to a message with `/debug` → debug info sent to operator

## Phase 3: Workers (requires operator to spawn)
- [ ] Ask operator to create task → worktree created, topic created
- [ ] Message in task topic → routed to worker pane
- [ ] Worker notification → appears in task topic
- [ ] Permission prompt Allow/Deny buttons work

## Phase 4: Recovery
- [ ] `/status` shows tasks
- [ ] Delete `operator/registry.json`
- [ ] `/recover` → finds tasks from marker files
- [ ] `/status` shows recovered tasks

## Phase 5: Polish
- [ ] Pause task → topic renamed with ⏸️
- [ ] Resume task → topic renamed with ▶️
- [ ] Cleanup task → topic renamed with ✅
- [ ] `.claude-army-setup.sh` in repo → runs after worktree creation

## Edge Cases
- [ ] Kill operator tmux session → resurrects on next message
- [ ] Kill worker tmux session → resurrects on next message to that topic
- [ ] Daemon restart → picks up existing sessions
- [ ] Multiple permission prompts → all show, handled correctly

## Bot Commands Quick Reference
```
/setup   - Initialize group
/reset   - Clear configuration
/status  - Show all tasks
/recover - Rebuild registry from marker files
/help    - Show commands
/todo    - Add todo to operator
/debug   - Debug a message (reply)
```
