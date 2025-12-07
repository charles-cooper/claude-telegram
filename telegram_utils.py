"""Shared utilities for Telegram integration."""

import datetime
import difflib
import json
import os
import requests
import subprocess
import threading
from pathlib import Path

_log_lock = threading.Lock()


def log(msg: str):
    """Print with timestamp (milliseconds). Thread-safe."""
    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    with _log_lock:
        print(f"[{ts}] {msg}", flush=True)

CONFIG_FILE = Path.home() / "telegram.json"
STATE_FILE = Path("/tmp/claude-telegram-state.json")


class State:
    """Persistent dict with auto-flush on modification."""

    def __init__(self):
        self._data = self._read()

    def _read(self) -> dict:
        if not STATE_FILE.exists():
            return {}
        try:
            return json.loads(STATE_FILE.read_text())
        except:
            return {}

    def _flush(self):
        STATE_FILE.write_text(json.dumps(self._data))

    def get(self, msg_id: str) -> dict | None:
        """Get entry by message ID."""
        return self._data.get(str(msg_id))

    def __contains__(self, msg_id: str) -> bool:
        return str(msg_id) in self._data

    def __iter__(self):
        return iter(self._data)

    def items(self):
        return self._data.items()

    def add(self, msg_id: str, entry: dict):
        """Add entry and flush."""
        self._data[str(msg_id)] = entry
        self._flush()

    def update(self, msg_id: str, **fields):
        """Update fields on entry and flush."""
        if str(msg_id) in self._data:
            self._data[str(msg_id)].update(fields)
            self._flush()

    def remove(self, msg_id: str):
        """Remove entry and flush."""
        if str(msg_id) in self._data:
            del self._data[str(msg_id)]
            self._flush()

    @property
    def data(self) -> dict:
        """Get raw data dict (read-only view for iteration)."""
        return self._data


def strip_home(path: str) -> str:
    """Remove home directory prefix from path."""
    return path.removeprefix(str(Path.home()) + "/")


def escape_markdown_v2(text: str) -> str:
    """Escape ALL MarkdownV2 special chars in plain text.

    Use this for text that will appear OUTSIDE code blocks.
    Do NOT use on text that contains code blocks - escape pieces before assembly instead.
    """
    # All MarkdownV2 special chars - backslash first to avoid double-escaping
    special_chars = ['\\', '_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for char in special_chars:
        text = text.replace(char, '\\' + char)
    return text


def format_tool_permission(tool_name: str, tool_input: dict, markdown_v2: bool = False) -> str:
    """Format a tool call for display.

    If markdown_v2=True, escapes text outside code blocks for MarkdownV2.
    """
    def esc(s: str) -> str:
        """Escape text for MarkdownV2 if enabled."""
        return escape_markdown_v2(s) if markdown_v2 else s

    if tool_name == "Bash":
        cmd = tool_input.get("command", "").replace("```", "'''")
        desc = tool_input.get("description", "")
        desc_line = f"\n\n_{esc(desc)}_" if desc else ""
        return f"{esc('Claude is asking permission to run:')}\n\n```bash\n{cmd}\n```{desc_line}"

    elif tool_name == "Edit":
        fp = strip_home(tool_input.get("file_path", ""))
        old = tool_input.get("old_string", "")
        new = tool_input.get("new_string", "")
        diff = "\n".join(
            line.rstrip() for line in difflib.unified_diff(
                old.splitlines(), new.splitlines(),
                fromfile=fp, tofile=fp, n=9999
            )
        )
        diff = diff.replace("```", "'''")
        return f"{esc('Claude is asking permission to edit')} `{esc(fp)}`{esc(':')}\n\n```diff\n{diff}\n```"

    elif tool_name == "Write":
        fp = strip_home(tool_input.get("file_path", ""))
        content = tool_input.get("content", "").replace("```", "'''")
        return f"{esc('Claude is asking permission to write')} `{esc(fp)}`{esc(':')}\n\n```\n{content}\n```"

    elif tool_name == "Read":
        fp = strip_home(tool_input.get("file_path", ""))
        return f"{esc('Claude is asking permission to read')} `{esc(fp)}`"

    elif tool_name == "AskUserQuestion":
        questions = tool_input.get("questions", [])
        lines = [f"{esc('Claude is asking:')}\n"]
        for q in questions:
            question_text = q.get('question', '')
            lines.append(f"*{esc(question_text)}*\n")
            for opt in q.get("options", []):
                label = opt.get('label', '')
                lines.append(f"{esc('•')} {esc(label)}")
        return "\n".join(lines)

    else:
        input_str = json.dumps(tool_input, indent=2).replace("```", "'''")
        return f"{esc('Claude is asking permission to use')} {esc(tool_name)}{esc(':')}\n\n```\n{input_str}\n```"


def pane_exists(pane: str) -> bool:
    """Check if a tmux pane exists."""
    result = subprocess.run(
        ["tmux", "has-session", "-t", pane],
        capture_output=True
    )
    return result.returncode == 0


def send_telegram(bot_token: str, chat_id: str, msg: str, tool_name: str = None, reply_markup: dict = None, parse_mode: str = "Markdown") -> dict | None:
    """Send message to Telegram. Returns response JSON on success.

    parse_mode: "Markdown", "MarkdownV2", or "HTML"
    For MarkdownV2, caller must escape text pieces before assembly (use escape_markdown_v2).
    """
    payload = {"chat_id": chat_id, "text": msg, "parse_mode": parse_mode}

    if reply_markup:
        payload["reply_markup"] = reply_markup

    resp = requests.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        json=payload
    )

    # If markdown parsing fails, retry without parse_mode
    if resp.status_code == 400 and "can't parse entities" in resp.text:
        del payload["parse_mode"]
        resp = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json=payload
        )

    if not resp.ok:
        return None

    return resp.json()


def answer_callback(bot_token: str, callback_id: str, text: str = None):
    """Answer a callback query to dismiss the loading state."""
    requests.post(
        f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery",
        json={"callback_query_id": callback_id, "text": text}
    )


def send_reply(bot_token: str, chat_id: str, reply_to_msg_id: int, text: str):
    """Send a reply message on Telegram."""
    requests.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "reply_to_message_id": reply_to_msg_id}
    )


def update_message_buttons(bot_token: str, chat_id: str, msg_id: int, label: str):
    """Update message to show a single button with given label."""
    requests.post(
        f"https://api.telegram.org/bot{bot_token}/editMessageReplyMarkup",
        json={
            "chat_id": chat_id,
            "message_id": msg_id,
            "reply_markup": {"inline_keyboard": [[{"text": label, "callback_data": "_"}]]}
        }
    )


def delete_message(bot_token: str, chat_id: str, msg_id: int) -> bool:
    """Delete a message. Returns True if successful."""
    resp = requests.post(
        f"https://api.telegram.org/bot{bot_token}/deleteMessage",
        json={"chat_id": chat_id, "message_id": msg_id}
    )
    return resp.ok


def react_to_message(bot_token: str, chat_id: str, msg_id: int, emoji: str = "👀"):
    """React to a message with an emoji."""
    requests.post(
        f"https://api.telegram.org/bot{bot_token}/setMessageReaction",
        json={
            "chat_id": chat_id,
            "message_id": msg_id,
            "reaction": [{"type": "emoji", "emoji": emoji}]
        }
    )


def register_bot_commands(bot_token: str):
    """Register bot commands with Telegram. Raises on failure."""
    commands = [
        {"command": "debug", "description": "Debug a message (reply to it)"},
        {"command": "todo", "description": "Add a todo item for Claude"}
    ]
    resp = requests.post(
        f"https://api.telegram.org/bot{bot_token}/setMyCommands",
        json={"commands": commands}
    )
    resp.raise_for_status()
    log("Registered bot commands")
