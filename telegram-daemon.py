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
    send_telegram, log, update_message_buttons, delete_message,
    register_bot_commands
)
from transcript_watcher import TranscriptManager, PendingTool, CompactionEvent, IdleEvent
from telegram_poller import TelegramPoller

CONFIG_FILE = Path.home() / "telegram.json"
PID_FILE = Path("/tmp/claude-telegram-daemon.pid")

CLEANUP_INTERVAL = 300  # 5 minutes


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


def expire_old_buttons(bot_token: str, chat_id: str, pane: str, state: State, transcript_mgr):
    """Expire buttons for old messages on this pane if their tool_use has a result.

    Only expires if the tool was already handled (has tool_result in transcript).
    This avoids expiring legitimately pending prompts when Claude queues multiple tool_uses.
    """
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
                update_message_buttons(bot_token, chat_id, int(msg_id), "⏰ Expired")
                state.update(msg_id, handled=True)


# If tool_result arrives within this time, delete notification (quick response)
# If longer, mark expired (user may want to see what happened)
QUICK_RESPONSE_THRESHOLD = 4.0  # seconds

# If idle message gets superseded by tool_use within this time, delete it
IDLE_SUPERSESSION_THRESHOLD = 4.0  # seconds


def handle_superseded_idle(state: State, transcript_mgr):
    """Mark idle notifications that got superseded by tool_use.

    We keep superseded messages in state so users can still reply to them.
    """
    for msg_id, entry in list(state.items()):
        if entry.get("type") != "idle":
            continue
        if entry.get("superseded"):
            continue  # Already marked
        claude_msg_id = entry.get("claude_msg_id")
        if not claude_msg_id:
            continue
        # Check if this claude message now has tool_use in any watcher
        for watcher in transcript_mgr.watchers.values():
            if claude_msg_id in watcher.tool_use_msg_ids:
                state.update(msg_id, superseded=True)
                log(f"Idle superseded by tool_use: msg_id={msg_id}")
                break


def handle_completed_tools(bot_token: str, chat_id: str, state: State, transcript_mgr):
    """Handle notifications for tools that completed. Delete if quick, expire if slow."""
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
                    if delete_message(bot_token, chat_id, int(msg_id)):
                        log(f"Deleted (quick response {elapsed:.1f}s): msg_id={msg_id}")
                    else:
                        log(f"Failed to delete msg_id={msg_id}")
                    state.remove(msg_id)
                else:
                    # Slow response - mark expired so user can see what happened
                    update_message_buttons(bot_token, chat_id, int(msg_id), "⏰ Expired")
                    state.update(msg_id, handled=True)
                    log(f"Expired (slow response {elapsed:.1f}s): msg_id={msg_id}")


def send_compaction_notification(bot_token: str, chat_id: str, event: CompactionEvent):
    """Send Telegram notification for a compaction event."""
    project = escape_markdown_v2(strip_home(event.cwd))
    trigger = escape_markdown_v2(event.trigger)
    tokens = escape_markdown_v2(f"{event.pre_tokens:,}")
    msg = f"`{project}`\n\n🔄 Context compacted \\({trigger}, {tokens} tokens\\)"
    send_telegram(bot_token, chat_id, msg, parse_mode="MarkdownV2")
    log(f"Notified: compaction ({event.trigger})")


def send_idle_notification(bot_token: str, chat_id: str, event: IdleEvent, state: State) -> int | None:
    """Send Telegram notification when Claude is waiting for input. Returns message_id."""
    project = escape_markdown_v2(strip_home(event.cwd))
    text = escape_markdown_v2(event.text)
    msg = f"`{project}`\n\n💬 {text}"
    result = send_telegram(bot_token, chat_id, msg, parse_mode="MarkdownV2")
    if not result:
        return None
    msg_id = result.get("result", {}).get("message_id")
    if msg_id and event.msg_id:
        state.add(msg_id, {
            "pane": event.pane,
            "type": "idle",
            "claude_msg_id": event.msg_id,
            "cwd": event.cwd,
            "notified_at": time.time()
        })
        log(f"Notified: idle (msg_id={msg_id}, claude_msg_id={event.msg_id[:20]}...)")
    else:
        log(f"Notified: idle")
    return msg_id


def send_notification(bot_token: str, chat_id: str, tool: PendingTool, state: State) -> int | None:
    """Send Telegram notification for a pending tool. Returns message_id."""
    project = escape_markdown_v2(strip_home(tool.cwd))
    assistant_text = escape_markdown_v2(tool.assistant_text) if tool.assistant_text else ""
    prefix = f"{assistant_text}\n\n\\-\\-\\-\n\n" if assistant_text else ""
    tool_desc = format_tool_permission(tool.tool_name, tool.tool_input, markdown_v2=True)
    msg = f"`{project}`\n\n{prefix}{tool_desc}"

    reply_markup = {
        "inline_keyboard": [[
            {"text": "Allow", "callback_data": "y"},
            {"text": "Deny", "callback_data": "n"}
        ]]
    }

    result = send_telegram(bot_token, chat_id, msg, tool.tool_name, reply_markup, parse_mode="MarkdownV2")
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

    last_cleanup = time.time()
    last_discover = time.time()

    log("Watching transcripts and polling Telegram...")

    while True:
        try:
            now = time.time()

            # Periodic discovery of new transcripts (every 30 seconds)
            if now - last_discover > 30:
                transcript_mgr.discover_transcripts()
                last_discover = now

            # Check transcripts for new tool_use, compactions, and idle events
            pending_tools, compactions, idle_events = transcript_mgr.check_all()
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
            handle_completed_tools(bot_token, chat_id, state, transcript_mgr)

            # Handle superseded idle notifications (mark, don't remove)
            handle_superseded_idle(state, transcript_mgr)
            for pane in transcript_mgr.pane_to_transcript:
                expire_old_buttons(bot_token, chat_id, pane, state, transcript_mgr)

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
