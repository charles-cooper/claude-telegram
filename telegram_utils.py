"""Shared utilities for Telegram integration."""

import datetime
import difflib
import json
import os
import requests
import shlex
import subprocess
import threading
from pathlib import Path


def shell_quote(s: str) -> str:
    """Quote a string for safe shell use."""
    return shlex.quote(s)


class TopicCreationError(Exception):
    """Failed to create Telegram topic."""
    pass


class NoTopicRightsError(TopicCreationError):
    """Bot lacks permission to create topics."""
    pass

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


def send_chat_action(bot_token: str, chat_id: str, action: str = "typing", topic_id: int = None):
    """Send chat action (typing indicator). Disappears after 5s or when message sent."""
    payload = {"chat_id": chat_id, "action": action}
    if topic_id:
        payload["message_thread_id"] = topic_id
    requests.post(
        f"https://api.telegram.org/bot{bot_token}/sendChatAction",
        json=payload
    )


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
        {"command": "setup", "description": "Initialize this group as control center"},
        {"command": "reset", "description": "Remove configuration"},
        {"command": "status", "description": "Show all tasks and status"},
        {"command": "recover", "description": "Rebuild registry from marker files"},
        {"command": "help", "description": "Show available commands"},
        {"command": "todo", "description": "Add todo to Operator queue"},
        {"command": "debug", "description": "Debug a message (reply to it)"},
    ]
    resp = requests.post(
        f"https://api.telegram.org/bot{bot_token}/setMyCommands",
        json={"commands": commands}
    )
    resp.raise_for_status()
    log("Registered bot commands")


# ============ Forum API Functions ============

def get_chat(bot_token: str, chat_id: str) -> dict | None:
    """Get chat info. Returns None on error."""
    resp = requests.post(
        f"https://api.telegram.org/bot{bot_token}/getChat",
        json={"chat_id": chat_id}
    )
    if not resp.ok:
        return None
    return resp.json().get("result")


def is_forum_enabled(bot_token: str, chat_id: str) -> bool:
    """Check if chat is a forum (supergroup with topics enabled)."""
    chat = get_chat(bot_token, chat_id)
    if not chat:
        return False
    return chat.get("is_forum", False)


def create_forum_topic(bot_token: str, chat_id: str, name: str, icon_color: int = None) -> dict:
    """Create a forum topic. Returns topic info. Raises TopicCreationError on failure.

    icon_color options: 0x6FB9F0 (blue), 0xFFD67E (yellow), 0xCB86DB (purple),
                        0x8EEE98 (green), 0xFF93B2 (pink), 0xFB6F5F (red)
    """
    payload = {"chat_id": chat_id, "name": name}
    if icon_color:
        payload["icon_color"] = icon_color

    resp = requests.post(
        f"https://api.telegram.org/bot{bot_token}/createForumTopic",
        json=payload
    )
    if not resp.ok:
        log(f"Failed to create topic '{name}': {resp.text}")
        if "not enough rights" in resp.text:
            raise NoTopicRightsError(resp.text)
        raise TopicCreationError(resp.text)
    return resp.json().get("result")


def close_forum_topic(bot_token: str, chat_id: str, topic_id: int) -> bool:
    """Close a forum topic. Returns True on success."""
    resp = requests.post(
        f"https://api.telegram.org/bot{bot_token}/closeForumTopic",
        json={"chat_id": chat_id, "message_thread_id": topic_id}
    )
    return resp.ok


def reopen_forum_topic(bot_token: str, chat_id: str, topic_id: int) -> bool:
    """Reopen a closed forum topic. Returns True on success."""
    resp = requests.post(
        f"https://api.telegram.org/bot{bot_token}/reopenForumTopic",
        json={"chat_id": chat_id, "message_thread_id": topic_id}
    )
    return resp.ok


def edit_forum_topic(bot_token: str, chat_id: str, topic_id: int, name: str = None) -> bool:
    """Edit a forum topic name. Returns True on success."""
    payload = {"chat_id": chat_id, "message_thread_id": topic_id}
    if name:
        payload["name"] = name
    resp = requests.post(
        f"https://api.telegram.org/bot{bot_token}/editForumTopic",
        json=payload
    )
    return resp.ok


def send_to_topic(bot_token: str, chat_id: str, topic_id: int, text: str,
                  reply_markup: dict = None, parse_mode: str = "MarkdownV2") -> dict | None:
    """Send message to a specific forum topic. Returns response JSON on success.

    For the General topic (topic_id=1), don't pass message_thread_id as Telegram
    expects messages to the General topic to be sent without it.
    """
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode
    }
    # General topic (1) doesn't use message_thread_id
    if topic_id and topic_id != 1:
        payload["message_thread_id"] = topic_id
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
        log(f"Failed to send to topic {topic_id}: {resp.text}")
        return None
    return resp.json()


def pin_message(bot_token: str, chat_id: str, msg_id: int, disable_notification: bool = True) -> bool:
    """Pin a message in a chat. Returns True on success."""
    resp = requests.post(
        f"https://api.telegram.org/bot{bot_token}/pinChatMessage",
        json={
            "chat_id": chat_id,
            "message_id": msg_id,
            "disable_notification": disable_notification
        }
    )
    return resp.ok


def get_chat_administrators(bot_token: str, chat_id: str) -> list | None:
    """Get list of chat administrators. Returns None on error."""
    resp = requests.post(
        f"https://api.telegram.org/bot{bot_token}/getChatAdministrators",
        json={"chat_id": chat_id}
    )
    if not resp.ok:
        return None
    return resp.json().get("result", [])
