#!/usr/bin/env python3
"""Telegram daemon - watches transcripts and polls Telegram.

Main loop:
1. Poll transcripts for new tool_use entries (every ~1 second)
2. Poll Telegram for responses (5 second timeout)
3. Send notifications for pending tools
4. Handle Telegram callbacks and messages
"""

import atexit
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

from telegram_utils import (
    State, pane_exists,
    format_tool_permission, strip_home, escape_markdown_v2,
    send_telegram, send_to_topic, send_chat_action, log, update_message_buttons, delete_message,
    register_bot_commands, NoTopicRightsError, TopicCreationError
)
from transcript_watcher import TranscriptManager, PendingTool, CompactionEvent, IdleEvent, ActivityInfo
from telegram_poller import TelegramPoller
from registry import get_config, get_registry, is_managed_directory, read_marker_file
from session_operator import is_operator_pane
from session_worker import is_worker_pane, register_existing_session

CONFIG_FILE = Path.home() / "telegram.json"
PID_FILE = Path("/tmp/claude-telegram-daemon.pid")

CLEANUP_INTERVAL = 300  # 5 minutes

# Track if we've warned about permissions (avoid spam)
_permission_warning_sent = False


def try_auto_register(cwd: str, pane: str, bot_token: str, group_id: str) -> dict | None:
    """Try to auto-register a session. Returns task_data or None. Sends permission warning if needed."""
    global _permission_warning_sent

    registry = get_registry()

    # Generate unique name
    task_name = Path(cwd).name
    base_name = task_name
    counter = 1
    while registry.get_task(task_name):
        task_name = f"{base_name}-{counter}"
        counter += 1

    try:
        task_data = register_existing_session(cwd, task_name)
        if task_data:
            task_data["pane"] = pane
            registry.add_task(task_name, task_data)
            log(f"Auto-registered session: {task_name}")
            return task_data
    except NoTopicRightsError:
        if not _permission_warning_sent:
            _permission_warning_sent = True
            msg = (
                "⚠️ *Cannot create topics*\n\n"
                "Bot needs admin rights with _Manage Topics_ permission.\n\n"
                "Group Settings → Administrators → Bot → Enable *Manage Topics*"
            )
            result = send_telegram(bot_token, group_id, msg)
            if result:
                log("Sent permission warning to group")
            else:
                log("Failed to send permission warning")
    except TopicCreationError as e:
        log(f"Failed to auto-register {task_name}: {e}")

    return None


class DaemonAlreadyRunning(Exception):
    pass


class TmuxNotAvailable(Exception):
    pass


def handle_sigterm(signum, frame):
    """Handle SIGTERM by exiting cleanly."""
    sys.exit(0)


def check_singleton():
    """Ensure only one daemon is running."""
    if PID_FILE.exists():
        pid = int(PID_FILE.read_text().strip())
        # Check if process is still running
        try:
            os.kill(pid, 0)
            raise DaemonAlreadyRunning(f"Daemon already running with PID {pid}")
        except OSError:
            # Process not running, stale PID file
            pass
    # Write our PID
    PID_FILE.write_text(str(os.getpid()))
    atexit.register(PID_FILE.unlink, missing_ok=True)
    signal.signal(signal.SIGTERM, handle_sigterm)


def check_tmux():
    """Verify tmux is available."""
    result = subprocess.run(["tmux", "list-sessions"], capture_output=True)
    if result.returncode != 0:
        raise TmuxNotAvailable("tmux not available or no sessions running")


def cleanup_dead_panes(state: State):
    """Remove entries for panes that no longer exist."""
    dead = []
    for msg_id, entry in state.items():
        pane = entry.get("pane")
        if not pane or not pane_exists(pane):
            dead.append(msg_id)
    for msg_id in dead:
        state.remove(msg_id)
    return len(dead)


def expire_old_buttons(bot_token: str, pane: str, state: State, transcript_mgr):
    """Expire buttons for old messages on this pane if their tool_use has a result.

    Only expires if the tool was already handled (has tool_result in transcript).
    This avoids expiring legitimately pending prompts when Claude queues multiple tool_uses.
    """
    config = get_config()
    if not config.is_configured():
        return
    group_id = str(config.group_id)

    for msg_id, entry in list(state.items()):
        if entry.get("pane") != pane or entry.get("handled"):
            continue
        if entry.get("type") != "permission_prompt":
            continue
        tool_use_id = entry.get("tool_use_id")
        transcript_path = entry.get("transcript_path")
        if not tool_use_id or not transcript_path:
            continue
        # Check if this tool has a result in transcript
        if transcript_path in transcript_mgr.watchers:
            watcher = transcript_mgr.watchers[transcript_path]
            if tool_use_id in watcher.tool_results:
                update_message_buttons(bot_token, group_id, int(msg_id), "⏰ Expired")
                state.update(msg_id, handled=True)


# If tool_result arrives within this time, delete notification (quick response)
# If longer, mark expired (user may want to see what happened)
QUICK_RESPONSE_THRESHOLD = 4.0  # seconds

# If idle message gets superseded by tool_use within this time, delete it
IDLE_SUPERSESSION_THRESHOLD = 4.0  # seconds


def handle_superseded_idle(bot_token: str, state: State, transcript_mgr):
    """Handle idle notifications that got superseded by tool_use.

    If tool_use appears within 4 seconds, delete the notification (false positive).
    Otherwise, keep for user reply capability.
    """
    config = get_config()
    if not config.is_configured():
        return
    group_id = str(config.group_id)
    now = time.time()

    for msg_id, entry in list(state.items()):
        if entry.get("type") != "idle":
            continue
        if entry.get("superseded"):
            continue  # Already handled
        claude_msg_id = entry.get("claude_msg_id")
        if not claude_msg_id:
            continue
        # Check if this claude message now has tool_use in any watcher
        for watcher in transcript_mgr.watchers.values():
            if claude_msg_id in watcher.tool_use_msg_ids:
                notified_at = entry.get("notified_at", 0)
                elapsed = now - notified_at if notified_at else 999

                if elapsed < IDLE_SUPERSESSION_THRESHOLD:
                    # Quick supersession - delete notification (was false positive)
                    if delete_message(bot_token, group_id, int(msg_id)):
                        log(f"Deleted idle (superseded in {elapsed:.1f}s): msg_id={msg_id}")
                    state.remove(msg_id)
                else:
                    # Slow supersession - keep for user reply capability
                    state.update(msg_id, superseded=True)
                    log(f"Idle superseded (after {elapsed:.1f}s): msg_id={msg_id}")
                break


def handle_completed_tools(bot_token: str, state: State, transcript_mgr):
    """Handle notifications for tools that completed. Delete if quick, expire if slow."""
    config = get_config()
    if not config.is_configured():
        return
    group_id = str(config.group_id)

    now = time.time()
    for msg_id, entry in list(state.items()):
        if entry.get("handled"):
            continue
        tool_use_id = entry.get("tool_use_id")
        if not tool_use_id:
            continue
        # Check if this tool has a result in any watcher
        transcript_path = entry.get("transcript_path")
        if transcript_path and transcript_path in transcript_mgr.watchers:
            watcher = transcript_mgr.watchers[transcript_path]
            if tool_use_id in watcher.tool_results:
                notified_at = entry.get("notified_at", 0)
                elapsed = now - notified_at if notified_at else 999

                if elapsed < QUICK_RESPONSE_THRESHOLD:
                    # Quick response - delete notification
                    if delete_message(bot_token, group_id, int(msg_id)):
                        log(f"Deleted (quick response {elapsed:.1f}s): msg_id={msg_id}")
                    else:
                        log(f"Failed to delete msg_id={msg_id}")
                    state.remove(msg_id)
                else:
                    # Slow response - mark expired so user can see what happened
                    update_message_buttons(bot_token, group_id, int(msg_id), "⏰ Expired")
                    state.update(msg_id, handled=True)
                    log(f"Expired (slow response {elapsed:.1f}s): msg_id={msg_id}")


def send_to_chat_or_topic(bot_token: str, chat_id: str, pane: str, cwd: str, msg: str,
                          reply_markup: dict = None, parse_mode: str = "MarkdownV2") -> dict | None:
    """Send message to appropriate destination based on pane/cwd.

    Routing priority:
    1. Operator pane → General topic
    2. Known task (by path) → task topic
    3. Managed directory (marker exists) → recover to registry, use topic
    4. Unmanaged → auto-register, create topic
    5. Auto-registration failed → General topic (for debugging)
    """
    config = get_config()
    if not config.is_configured():
        return send_telegram(bot_token, chat_id, msg, None, reply_markup, parse_mode)

    group_id = str(config.group_id)

    # 1. Operator pane -> General topic
    if is_operator_pane(pane):
        return send_to_topic(bot_token, group_id, config.general_topic_id,
                            msg, reply_markup, parse_mode)

    registry = get_registry()

    # 2. Known task by path -> task topic
    result = registry.find_task_by_path(cwd)
    if result:
        task_name, task_data = result
        # Update pane if changed
        if task_data.get("pane") != pane:
            task_data["pane"] = pane
            registry.add_task(task_name, task_data)
        topic_id = task_data.get("topic_id")
        if topic_id:
            return send_to_topic(bot_token, group_id, topic_id, msg, reply_markup, parse_mode)

    # 3. Managed directory (marker exists but not in registry) -> recover
    marker = read_marker_file(cwd)
    if marker:
        task_name = marker.get("name")
        topic_id = marker.get("topic_id")
        if task_name and topic_id:
            # Recover to registry
            task_data = {
                "type": marker.get("type", "session"),
                "path": cwd,
                "topic_id": topic_id,
                "pane": pane,
                "status": "active",
            }
            if marker.get("repo"):
                task_data["repo"] = marker["repo"]
            registry.add_task(task_name, task_data)
            log(f"Recovered task from marker: {task_name}")
            return send_to_topic(bot_token, group_id, topic_id, msg, reply_markup, parse_mode)

    # 4. Unmanaged -> auto-register
    task_data = try_auto_register(cwd, pane, bot_token, group_id)
    if task_data:
        topic_id = task_data.get("topic_id")
        if topic_id:
            return send_to_topic(bot_token, group_id, topic_id, msg, reply_markup, parse_mode)

    # 5. Auto-registration failed -> General topic for debugging
    log(f"Auto-registration failed for {cwd}, falling back to General")
    return send_to_topic(bot_token, group_id, config.general_topic_id, msg, reply_markup, parse_mode)


def auto_register_discovered_sessions(bot_token: str, chat_id: str, transcript_mgr):
    """Auto-register newly discovered sessions that aren't in registry."""
    config = get_config()
    if not config.is_configured():
        return

    registry = get_registry()
    group_id = str(config.group_id)

    for transcript_path, watcher in transcript_mgr.watchers.items():
        cwd = watcher.cwd
        pane = watcher.pane

        # Skip operator pane
        if is_operator_pane(pane):
            continue

        # Skip if already known by path
        if registry.find_task_by_path(cwd):
            continue

        # Skip if marker exists (will be recovered on first notification)
        if is_managed_directory(cwd):
            continue

        # Auto-register this session
        try_auto_register(cwd, pane, bot_token, group_id)


def send_compaction_notification(bot_token: str, chat_id: str, event: CompactionEvent):
    """Send Telegram notification for a compaction event."""
    msg = f"🔄 Context compacted ({event.trigger}, {event.pre_tokens:,} tokens)"
    send_to_chat_or_topic(bot_token, chat_id, event.pane, event.cwd, msg, parse_mode="Markdown")
    log(f"Notified: compaction ({event.trigger})")


def send_typing_indicator(bot_token: str, activity: ActivityInfo):
    """Send typing indicator to the appropriate topic for this activity."""
    config = get_config()
    if not config.is_configured():
        return

    group_id = str(config.group_id)
    registry = get_registry()

    # Determine topic_id based on pane/cwd
    if is_operator_pane(activity.pane):
        topic_id = config.general_topic_id
    else:
        result = registry.find_task_by_path(activity.cwd)
        if result:
            _, task_data = result
            topic_id = task_data.get("topic_id")
        else:
            topic_id = config.general_topic_id

    send_chat_action(bot_token, group_id, "typing", topic_id)


def send_idle_notification(bot_token: str, chat_id: str, event: IdleEvent, state: State) -> int | None:
    """Send Telegram notification when Claude is waiting for input. Returns message_id."""
    msg = event.text
    result = send_to_chat_or_topic(bot_token, chat_id, event.pane, event.cwd, msg, parse_mode="Markdown")
    if not result:
        return None
    msg_id = result.get("result", {}).get("message_id")
    if msg_id and event.msg_id:
        state.add(msg_id, {
            "pane": event.pane,
            "type": "idle",
            "claude_msg_id": event.msg_id,
            "cwd": event.cwd,
            "transcript_path": event.transcript_path,
            "notified_at": time.time()
        })
        log(f"Notified: idle (msg_id={msg_id}, claude_msg_id={event.msg_id[:20]}...)")
    else:
        log(f"Notified: idle")
    return msg_id


def send_notification(bot_token: str, chat_id: str, tool: PendingTool, state: State) -> int | None:
    """Send Telegram notification for a pending tool. Returns message_id."""
    prefix = f"{tool.assistant_text}\n\n---\n\n" if tool.assistant_text else ""
    tool_desc = format_tool_permission(tool.tool_name, tool.tool_input, markdown_v2=False)
    msg = f"{prefix}{tool_desc}"

    reply_markup = {
        "inline_keyboard": [[
            {"text": "Allow", "callback_data": "y"},
            {"text": "Deny (or reply)", "callback_data": "n"}
        ]]
    }

    result = send_to_chat_or_topic(bot_token, chat_id, tool.pane, tool.cwd, msg, reply_markup, parse_mode="Markdown")
    if not result:
        return None

    msg_id = result.get("result", {}).get("message_id")
    if msg_id:
        state.add(msg_id, {
            "pane": tool.pane,
            "type": "permission_prompt",
            "transcript_path": tool.transcript_path,
            "tool_use_id": tool.tool_id,
            "tool_name": tool.tool_name,
            "cwd": tool.cwd,
            "notified_at": time.time()
        })
        log(f"Notified: {tool.tool_name} (msg_id={msg_id}, tool_id={tool.tool_id[:20]}...)")

    return msg_id


def main():
    check_singleton()
    check_tmux()

    config = json.loads(CONFIG_FILE.read_text())
    bot_token, chat_id = config["bot_token"], config["chat_id"]

    log(f"Starting daemon (PID {os.getpid()})...")
    register_bot_commands(bot_token)

    # Initialize components
    state = State()
    transcript_mgr = TranscriptManager()
    telegram_poller = TelegramPoller(bot_token, chat_id, state, timeout=30)
    update_queue = queue.Queue()

    def telegram_poll_thread():
        """Background thread for Telegram long-polling."""
        while True:
            try:
                updates = telegram_poller.poll()
                if updates:
                    update_queue.put(updates)
            except Exception as e:
                log(f"Telegram thread error: {e}")
                time.sleep(1)

    telegram_thread = threading.Thread(target=telegram_poll_thread, daemon=True)
    telegram_thread.start()

    # Bootstrap from state and discover transcripts
    transcript_mgr.add_from_state(state.data)
    transcript_mgr.discover_transcripts()
    auto_register_discovered_sessions(bot_token, chat_id, transcript_mgr)

    last_cleanup = time.time()
    last_discover = time.time()

    log("Watching transcripts and polling Telegram...")

    while True:
        try:
            now = time.time()

            # Periodic discovery of new transcripts (every 30 seconds)
            if now - last_discover > 30:
                transcript_mgr.discover_transcripts()
                auto_register_discovered_sessions(bot_token, chat_id, transcript_mgr)
                last_discover = now

            # Check transcripts for new tool_use, compactions, idle events, and activity
            pending_tools, compactions, idle_events, activity = transcript_mgr.check_all()

            # Send typing indicators FIRST (will be cancelled by subsequent messages)
            for act in activity:
                send_typing_indicator(bot_token, act)

            for tool in pending_tools:
                send_notification(bot_token, chat_id, tool, state)
            for event in compactions:
                send_compaction_notification(bot_token, chat_id, event)
            for event in idle_events:
                send_idle_notification(bot_token, chat_id, event, state)

            # Process any Telegram updates from background thread
            while not update_queue.empty():
                telegram_poller.process_updates(update_queue.get_nowait())

            # Handle completed tools (delete quick, expire slow)
            handle_completed_tools(bot_token, state, transcript_mgr)

            # Handle superseded idle notifications (delete if quick, mark if slow)
            handle_superseded_idle(bot_token, state, transcript_mgr)
            for pane in transcript_mgr.pane_to_transcript:
                expire_old_buttons(bot_token, pane, state, transcript_mgr)

            # Periodic cleanup (every 5 minutes)
            if now - last_cleanup > CLEANUP_INTERVAL:
                removed = cleanup_dead_panes(state)
                if removed:
                    log(f"Cleaned {removed} dead entries")
                transcript_mgr.cleanup_dead()
                last_cleanup = now

            time.sleep(0.1)

        except KeyboardInterrupt:
            break
        except Exception as e:
            log(f"Error: {e}")
            traceback.print_exc()


if __name__ == "__main__":
    main()
