#!/usr/bin/env python3
"""Daemon that polls Telegram for replies and sends them to Claude."""

import fcntl
import json
import requests
import subprocess
import sys
import time
from pathlib import Path

CONFIG_FILE = Path.home() / "telegram.json"
STATE_FILE = Path("/tmp/claude-telegram-state.json")
LOCK_FILE = Path("/tmp/claude-telegram-state.lock")


def read_state() -> dict:
    """Read state file with locking."""
    if not STATE_FILE.exists():
        return {}
    with open(LOCK_FILE, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        try:
            return json.loads(STATE_FILE.read_text())
        except:
            return {}


def write_state(state: dict):
    """Write state file with locking."""
    with open(LOCK_FILE, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        STATE_FILE.write_text(json.dumps(state))


class TmuxNotAvailable(Exception):
    pass


def check_tmux():
    """Verify tmux is available."""
    result = subprocess.run(["tmux", "list-sessions"], capture_output=True)
    if result.returncode != 0:
        raise TmuxNotAvailable("tmux not available or no sessions running")


def pane_exists(pane: str) -> bool:
    """Check if a tmux pane exists."""
    result = subprocess.run(
        ["tmux", "has-session", "-t", pane],
        capture_output=True
    )
    return result.returncode == 0


def send_to_pane(pane: str, text: str) -> bool:
    """Send text to a tmux pane."""
    try:
        # Regular input: clear line, send text, then Enter
        subprocess.run(["tmux", "send-keys", "-t", pane, "C-u"], check=True)
        subprocess.run(["tmux", "send-keys", "-t", pane, text], check=True)
        time.sleep(0.1)
        subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], check=True)
        return True
    except subprocess.CalledProcessError as e:
        print(f"  Error: {e}", flush=True)
        return False


def send_text_to_permission_prompt(pane: str, text: str) -> bool:
    """Send text reply to a permission prompt.

    Navigates to "Tell Claude something else" option, types text, submits.
    """
    try:
        subprocess.run(["tmux", "send-keys", "-t", pane, "C-u"], check=True)
        subprocess.run(["tmux", "send-keys", "-t", pane, "Down"], check=True)
        subprocess.run(["tmux", "send-keys", "-t", pane, "Down"], check=True)
        subprocess.run(["tmux", "send-keys", "-t", pane, text], check=True)
        time.sleep(0.1)
        subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], check=True)
        return True
    except subprocess.CalledProcessError as e:
        print(f"  Error: {e}", flush=True)
        return False


def send_permission_response(pane: str, allow: bool) -> bool:
    """Send permission response via arrow keys.

    Options are: 1) Yes  2) Yes+auto  3) Tell Claude something else
    Allow = Enter (select first option)
    Deny = Down Down Enter (select third option)
    """
    try:
        if allow:
            # First option is Yes - just press Enter
            subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], check=True)
        else:
            # Third option is "tell Claude something else" - Down Down Enter
            subprocess.run(["tmux", "send-keys", "-t", pane, "Down"], check=True)
            time.sleep(0.02)
            subprocess.run(["tmux", "send-keys", "-t", pane, "Down"], check=True)
            time.sleep(0.02)
            subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], check=True)
        return True
    except subprocess.CalledProcessError as e:
        print(f"  Error: {e}", flush=True)
        return False


def answer_callback(bot_token: str, callback_id: str, text: str = None):
    """Answer a callback query to dismiss the loading state."""
    requests.post(
        f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery",
        json={"callback_query_id": callback_id, "text": text}
    )


def update_message_after_action(bot_token: str, chat_id: str, msg_id: int, action: str):
    """Update message to show which action was taken."""
    if action == "y":
        label = "✓ Allowed"
    elif action == "n":
        label = "📝 Reply with instructions"
    elif action == "replied":
        label = "💬 Replied"
    else:
        label = "⏰ Expired"
    requests.post(
        f"https://api.telegram.org/bot{bot_token}/editMessageReplyMarkup",
        json={
            "chat_id": chat_id,
            "message_id": msg_id,
            "reply_markup": {"inline_keyboard": [[{"text": label, "callback_data": "_"}]]}
        }
    )


def cleanup_dead_panes(state: dict) -> dict:
    """Remove entries for panes that no longer exist."""
    live = {}
    for msg_id, entry in state.items():
        pane = entry.get("pane")
        if pane and pane_exists(pane):
            live[msg_id] = entry
    return live


def is_stale(msg_id: int, pane: str, state: dict) -> bool:
    """Check if a message is stale (newer message exists for same pane)."""
    latest = max(
        (int(mid) for mid, e in state.items() if e.get("pane") == pane),
        default=0
    )
    return msg_id < latest


def handle_permission_response(
    pane: str, response: str, bot_token: str, cb_id: str, chat_id, msg_id: int
) -> bool:
    """Handle y/n permission response using arrow key navigation.

    Returns True if successfully handled (should remove from state).
    """
    allow = (response == "y")
    label = "Allowed" if allow else "Denied"
    if send_permission_response(pane, allow):
        answer_callback(bot_token, cb_id, label)
        update_message_after_action(bot_token, chat_id, msg_id, response)
        print(f"  Sent {'Allow' if allow else 'Deny'} to pane {pane}", flush=True)
        return True
    else:
        answer_callback(bot_token, cb_id, "Failed: pane dead")
        print(f"  Failed (pane {pane} dead)", flush=True)
        return True  # Still remove from state - pane is dead


CLEANUP_INTERVAL = 300  # 5 minutes


def main():
    check_tmux()

    config = json.loads(CONFIG_FILE.read_text())
    bot_token, chat_id = config["bot_token"], config["chat_id"]

    print("Polling Telegram for replies...", flush=True)
    offset = 0
    last_cleanup = time.time()

    while True:
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{bot_token}/getUpdates",
                params={"offset": offset, "timeout": 30}
            )
            if not resp.ok:
                continue

            state = read_state()

            # Time-based cleanup
            if time.time() - last_cleanup > CLEANUP_INTERVAL:
                cleaned = cleanup_dead_panes(state)
                if len(cleaned) != len(state):
                    print(f"Cleaned {len(state) - len(cleaned)} dead entries", flush=True)
                    write_state(cleaned)
                    state = cleaned
                last_cleanup = time.time()

            for update in resp.json().get("result", []):
                offset = update["update_id"] + 1

                # Handle callback queries (button clicks)
                callback = update.get("callback_query")
                if callback:
                    cb_id = callback["id"]
                    cb_data = callback.get("data", "")
                    cb_msg = callback.get("message", {})
                    cb_msg_id = cb_msg.get("message_id")
                    cb_chat_id = cb_msg.get("chat", {}).get("id")
                    print(f"Callback: {cb_data} on msg_id={cb_msg_id}", flush=True)

                    if cb_data == "_":
                        answer_callback(bot_token, cb_id, "Already handled")
                        continue

                    if str(cb_msg_id) not in state:
                        answer_callback(bot_token, cb_id, "Session not found")
                        print(f"  Skipping: msg_id not in state", flush=True)
                        continue

                    entry = state[str(cb_msg_id)]
                    pane = entry["pane"]

                    if entry.get("handled"):
                        answer_callback(bot_token, cb_id, "Already handled")
                        print(f"  Already handled", flush=True)
                        continue

                    if is_stale(cb_msg_id, pane, state):
                        answer_callback(bot_token, cb_id, "Stale prompt")
                        update_message_after_action(bot_token, cb_chat_id, cb_msg_id, "stale")
                        state[str(cb_msg_id)]["handled"] = True
                        write_state(state)
                        print(f"  Stale prompt for pane {pane}", flush=True)
                        continue

                    is_permission = entry.get("type") == "permission_prompt"
                    if cb_data in ("y", "n"):
                        if is_permission:
                            if handle_permission_response(pane, cb_data, bot_token, cb_id, cb_chat_id, cb_msg_id):
                                state[str(cb_msg_id)]["handled"] = True
                                write_state(state)
                        else:
                            answer_callback(bot_token, cb_id, "No active prompt")
                            print(f"  Ignoring y/n: not a permission prompt", flush=True)
                    else:
                        if send_to_pane(pane, cb_data):
                            answer_callback(bot_token, cb_id, f"Sent: {cb_data}")
                            print(f"  Sent to pane {pane}: {cb_data}", flush=True)
                        else:
                            answer_callback(bot_token, cb_id, "Failed")
                            print(f"  Failed (pane {pane} dead)", flush=True)
                    continue

                # Handle regular messages
                msg = update.get("message", {})
                if not msg:
                    continue

                print(f"Update: {update.get('update_id')} msg_id={msg.get('message_id')}", flush=True)

                if str(msg.get("chat", {}).get("id")) != chat_id:
                    print(f"  Skipping: wrong chat", flush=True)
                    continue

                reply_to = msg.get("reply_to_message", {}).get("message_id")
                text = msg.get("text", "")
                print(f"  reply_to={reply_to} text={text[:30] if text else None}", flush=True)

                if reply_to and str(reply_to) in state and text:
                    entry = state[str(reply_to)]
                    pane = entry["pane"]
                    is_permission = entry.get("type") == "permission_prompt"

                    if is_permission:
                        success = send_text_to_permission_prompt(pane, text)
                    else:
                        success = send_to_pane(pane, text)

                    if success:
                        print(f"  Sent to pane {pane}: {text[:50]}...", flush=True)
                        if is_permission:
                            update_message_after_action(bot_token, chat_id, reply_to, "replied")
                            del state[str(reply_to)]
                            write_state(state)
                    else:
                        print(f"  Failed (pane {pane} dead)", flush=True)

        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
