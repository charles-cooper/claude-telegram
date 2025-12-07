"""Telegram poller - handles incoming messages and callbacks."""

import re
import subprocess
import time

import requests

from telegram_utils import (
    State, pane_exists, answer_callback, send_reply, update_message_buttons, log,
    react_to_message
)
from bot_commands import CommandHandler


def tool_already_handled(transcript_path: str, tool_use_id: str) -> bool:
    """Check if a tool_use has a corresponding tool_result in the transcript."""
    if not transcript_path or not tool_use_id:
        return False
    try:
        with open(transcript_path) as f:
            for line in f:
                if tool_use_id in line and '"tool_result"' in line:
                    return True
    except Exception as e:
        log(f"  Error checking transcript: {e}")
    return False


def get_pending_tool_from_transcript(transcript_path: str) -> str | None:
    """Check transcript for any pending tool_use (no corresponding tool_result)."""
    if not transcript_path:
        return None
    try:
        tool_uses = set()
        tool_results = set()
        with open(transcript_path) as f:
            for line in f:
                if '"tool_use"' in line and '"type":"tool_use"' in line:
                    match = re.search(r'"id"\s*:\s*"(toolu_[^"]+)"', line)
                    if match:
                        tool_uses.add(match.group(1))
                if '"tool_result"' in line:
                    match = re.search(r'"tool_use_id"\s*:\s*"(toolu_[^"]+)"', line)
                    if match:
                        tool_results.add(match.group(1))

        pending = tool_uses - tool_results
        if pending:
            return pending.pop()
    except Exception as e:
        log(f"  Error checking transcript for pending: {e}")
    return None


def send_to_pane(pane: str, text: str) -> bool:
    """Send text to a tmux pane."""
    try:
        subprocess.run(["tmux", "send-keys", "-t", pane, "C-u"], check=True)
        subprocess.run(["tmux", "send-keys", "-t", pane, "-l", text], check=True)
        time.sleep(0.1)
        subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], check=True)
        return True
    except subprocess.CalledProcessError as e:
        log(f"  Error: {e}")
        return False


def send_text_to_permission_prompt(pane: str, text: str) -> bool:
    """Send text reply to a permission prompt (option 3)."""
    try:
        subprocess.run(["tmux", "send-keys", "-t", pane, "C-u"], check=True)
        time.sleep(0.02)
        subprocess.run(["tmux", "send-keys", "-t", pane, "Down"], check=True)
        time.sleep(0.02)
        subprocess.run(["tmux", "send-keys", "-t", pane, "Down"], check=True)
        time.sleep(0.02)
        # Select option 3 to activate text input
        subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], check=True)
        time.sleep(0.1)
        subprocess.run(["tmux", "send-keys", "-t", pane, "-l", text], check=True)
        time.sleep(0.1)
        subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], check=True)
        return True
    except subprocess.CalledProcessError as e:
        log(f"  Error: {e}")
        return False


def send_permission_response(pane: str, response: str) -> bool:
    """Send permission response via arrow keys.
    y = Enter (option 1: Yes)
    a = Down Enter (option 2: Yes, don't ask again)
    n = Down Down Enter (option 3: Tell Claude something)
    """
    try:
        if response == "y":
            subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], check=True)
        elif response == "a":
            subprocess.run(["tmux", "send-keys", "-t", pane, "Down"], check=True)
            time.sleep(0.02)
            subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], check=True)
        else:  # n
            subprocess.run(["tmux", "send-keys", "-t", pane, "Down"], check=True)
            time.sleep(0.02)
            subprocess.run(["tmux", "send-keys", "-t", pane, "Down"], check=True)
            time.sleep(0.02)
            subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], check=True)
        return True
    except subprocess.CalledProcessError as e:
        log(f"  Error: {e}")
        return False


def get_action_label(action: str, tool_name: str = None) -> str:
    """Get button label for an action."""
    if action == "y":
        return "✓ Allowed"
    elif action == "a":
        return "✓ Always"
    elif action == "n":
        return "📝 Reply"
    elif action == "replied":
        return "💬 Replied"
    else:
        return "⏰ Expired"


class TelegramPoller:
    """Polls Telegram for updates and handles them."""

    def __init__(self, bot_token: str, chat_id: str, state: State, timeout: int = 5):
        # Telegram API requires timeout to be int >= 1
        assert isinstance(timeout, int) and timeout >= 1
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.state = state
        self.timeout = timeout
        self.offset = 0
        self.command_handler = CommandHandler(bot_token, chat_id, state)

    def poll(self) -> list[dict]:
        """Poll for new updates. Returns list of updates."""
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{self.bot_token}/getUpdates",
                params={"offset": self.offset, "timeout": self.timeout},
                timeout=self.timeout + 2
            )
            if not resp.ok:
                return []
            updates = resp.json().get("result", [])
            if updates:
                log(f"Got {len(updates)} updates")
            for update in updates:
                self.offset = update["update_id"] + 1
            return updates
        except Exception as e:
            log(f"Telegram poll error: {e}")
            return []

    def handle_callback(self, callback: dict):
        """Handle a callback query (button click)."""
        cb_id = callback["id"]
        cb_data = callback.get("data", "")
        cb_msg = callback.get("message", {})
        cb_msg_id = cb_msg.get("message_id")
        cb_chat_id = cb_msg.get("chat", {}).get("id")
        log(f"Callback: {cb_data} on msg_id={cb_msg_id}")

        if cb_data == "_":
            answer_callback(self.bot_token, cb_id, "Already handled")
            return

        msg_key = str(cb_msg_id)
        if msg_key not in self.state:
            answer_callback(self.bot_token, cb_id, "Session not found")
            log(f"  Skipping: msg_id not in state")
            return

        entry = self.state.get(msg_key)
        pane = entry.get("pane")

        if entry.get("handled"):
            answer_callback(self.bot_token, cb_id, "Already handled")
            log(f"  Already handled")
            return

        # Check if stale (newer message exists for same pane)
        latest = max(
            (int(mid) for mid, e in self.state.items() if e.get("pane") == pane),
            default=0
        )
        if cb_msg_id < latest:
            answer_callback(self.bot_token, cb_id, "Stale prompt")
            update_message_buttons(self.bot_token, cb_chat_id, cb_msg_id, "⏰ Expired")
            self.state.update(msg_key, handled=True)
            log(f"  Stale prompt for pane {pane}")
            return

        is_permission = entry.get("type") == "permission_prompt"

        # Check if tool was already handled via TUI
        transcript_path = entry.get("transcript_path")
        tool_use_id = entry.get("tool_use_id")
        if is_permission and tool_already_handled(transcript_path, tool_use_id):
            answer_callback(self.bot_token, cb_id, "Already handled in TUI")
            update_message_buttons(self.bot_token, cb_chat_id, cb_msg_id, "⏰ Expired")
            self.state.update(msg_key, handled=True)
            log(f"  Already handled in TUI (tool_use_id={tool_use_id})")
            return

        if cb_data in ("y", "n", "a"):
            if is_permission:
                tool_name = entry.get("tool_name")
                labels = {"y": "Allowed", "a": f"Always: {tool_name}" if tool_name else "Always allowed", "n": "Denied"}
                if send_permission_response(pane, cb_data):
                    answer_callback(self.bot_token, cb_id, labels[cb_data])
                    update_message_buttons(self.bot_token, cb_chat_id, cb_msg_id, get_action_label(cb_data, tool_name))
                    self.state.update(msg_key, handled=True)
                    log(f"  Sent {labels[cb_data]} to pane {pane}")
                else:
                    answer_callback(self.bot_token, cb_id, "Failed: pane dead")
                    self.state.update(msg_key, handled=True)
                    log(f"  Failed (pane {pane} dead)")
            else:
                answer_callback(self.bot_token, cb_id, "No active prompt")
                log(f"  Ignoring y/n/a: not a permission prompt")
        else:
            if send_to_pane(pane, cb_data):
                answer_callback(self.bot_token, cb_id, f"Sent: {cb_data}")
                log(f"  Sent to pane {pane}: {cb_data}")
            else:
                answer_callback(self.bot_token, cb_id, "Failed")
                log(f"  Failed (pane {pane} dead)")

    def handle_message(self, msg: dict):
        """Handle a regular message (text reply)."""
        msg_id = msg.get("message_id")
        chat_id = str(msg.get("chat", {}).get("id"))
        log(f"Message: msg_id={msg_id}")

        if chat_id != self.chat_id:
            log(f"  Skipping: wrong chat")
            return

        reply_to = msg.get("reply_to_message", {}).get("message_id")
        text = msg.get("text", "")
        log(f"  reply_to={reply_to} text={text[:30] if text else None}")

        # Handle bot commands (/debug, /todo, etc.)
        if self.command_handler.handle_command(msg):
            return

        if not reply_to or str(reply_to) not in self.state or not text:
            return

        entry = self.state.get(str(reply_to))
        pane = entry.get("pane")
        transcript_path = entry.get("transcript_path")

        if not pane:
            log(f"  Skipping: no pane in entry")
            return

        # Check transcript for pending tool_use
        pending_tool_id = get_pending_tool_from_transcript(transcript_path)

        if pending_tool_id:
            entry_tool_id = entry.get("tool_use_id")
            if entry_tool_id == pending_tool_id:
                # User is replying to the pending permission
                if send_text_to_permission_prompt(pane, text):
                    log(f"  Sent to permission prompt on pane {pane}: {text[:50]}...")
                    update_message_buttons(self.bot_token, self.chat_id, reply_to, "💬 Replied")
                    self.state.update(str(reply_to), handled=True)
                    react_to_message(self.bot_token, self.chat_id, msg_id)
                else:
                    log(f"  Failed (pane {pane} dead)")
            else:
                # Block: there's a different pending permission
                log(f"  Blocked: transcript has pending tool ({pending_tool_id[:20]}...), reply to that first")
                send_reply(self.bot_token, self.chat_id, msg_id, "⚠️ Ignored: there's a pending permission prompt. Please respond to that first.")
        else:
            # No pending permission - send as regular input
            if send_to_pane(pane, text):
                log(f"  Sent to pane {pane}: {text[:50]}...")
                react_to_message(self.bot_token, self.chat_id, msg_id)
            else:
                log(f"  Failed (pane {pane} dead)")

    def process_updates(self, updates: list[dict]):
        """Process a list of updates."""
        if not updates:
            return

        for update in updates:
            callback = update.get("callback_query")
            if callback:
                self.handle_callback(callback)
                continue

            msg = update.get("message", {})
            if msg:
                self.handle_message(msg)
