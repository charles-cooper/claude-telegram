# Claude Army TODO

## Pending Tasks

- [x] Add short-lived activity banner (like Allow toast) when Claude is working
- [ ] Operator chooses better names for auto-registered sessions
- [x] Refactor telegram_poller routing (extract permission reply handling)

## Async Todo Workflow
- [x] /todo should write to TODO.local.md in the task's working directory (not route to operator)
- [x] Claude instances should read TODO.local.md regularly and update their todo stack
- [ ] Claude should send TG message when it acknowledges new todos

## Task Directory Setup
- [x] Generate CLAUDE.local.md in each task directory on creation
  - Include instruction: "update me to propagate learnings to future sessions"
- [ ] Operator should be able to update the CLAUDE.local.md template (maybe?)

## Session Management Commands
- [x] /tmux - print tmux attach command for current task's session
- [x] /show - dump tmux pane output
- [ ] /clear-context - exit Claude and start new session without -r or -c (when Claude gets out of hand)
- [ ] /status - report Claude session status (edit mode? pending permission? idle?)

## Operator Improvements
- [ ] Operator should periodically dump tmux sessions / check logs to monitor work progress
- [ ] Operator should update its own CLAUDE.local.md with learnings about user and workflows

## Nice to Have
- [ ] Operator command to summarize all outstanding tasks and current status (help user stay organized)
- [ ] Handle conflicts between topic/task names (what if two tasks have same name?)
- [ ] Update topic name when waiting for user action (e.g., "🔴 task-name" when permission needed)

