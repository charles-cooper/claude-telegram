"""Shared utilities for Telegram integration."""

import datetime
import difflib
import json
import os
import requests
import shlex
import subprocess
import threading
import time
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


def _parse_code_blocks(text: str) -> list[dict]:
    """Parse all code blocks in the message.

    Returns list of dicts with: {"start": int, "end": int, "language": str, "closed": bool}
    """
    blocks = []
    pos = 0

    while True:
        # Find opening ```
        start = text.find('```', pos)
        if start == -1:
            break

        # Extract language (rest of line after ```)
        line_end = text.find('\n', start)
        if line_end == -1:
            # Malformed: ``` at end of text without newline
            blocks.append({"start": start, "end": len(text), "language": "", "closed": False})
            break

        language = text[start + 3:line_end].strip()

        # Find closing ```
        end = text.find('\n```', line_end)
        if end == -1:
            # Unclosed block
            blocks.append({"start": start, "end": len(text), "language": language, "closed": False})
            break

        # Include the closing ``` in the block
        blocks.append({"start": start, "end": end + 4, "language": language, "closed": True})
        pos = end + 4

    return blocks


def _find_split_point(text: str, start: int, target: int, code_blocks: list[dict]) -> dict:
    """Find optimal split point, preferring natural boundaries.

    Returns: {"position": int, "in_code_block": bool, "block_info": dict | None}
    """
    # Check if we're in a code block at target position
    in_block = None
    for block in code_blocks:
        if block["start"] <= target <= block["end"]:
            in_block = block
            break

    # Search backward for best split point
    # Priority 1: Double newline (paragraph break) outside code
    if not in_block:
        for offset in range(target, start, -1):
            if offset > 0 and text[offset - 1:offset + 1] == '\n\n':
                return {"position": offset, "in_code_block": False, "block_info": None}

    # Priority 2: Single newline outside code
    if not in_block:
        for offset in range(target, start, -1):
            if text[offset] == '\n':
                return {"position": offset, "in_code_block": False, "block_info": None}

    # Priority 3: Single newline inside code (if we must)
    if in_block:
        for offset in range(target, max(start, in_block["start"]), -1):
            if text[offset] == '\n':
                return {"position": offset, "in_code_block": True, "block_info": in_block}

    # Last resort: hard split at target
    return {"position": target, "in_code_block": in_block is not None, "block_info": in_block}


def split_message_with_code_blocks(text: str, max_length: int = 4096) -> list[str]:
    """Split message into chunks that fit Telegram's limit, preserving code blocks.

    - Splits messages exceeding max_length into multiple chunks
    - Properly closes and reopens code blocks at split points
    - Adds continuation markers like "(1/3)", "(2/3)" to each chunk
    - Returns list of message chunks
    """
    # Safety margin for code block tags and continuation markers
    SAFE_LIMIT = 4000

    # Parse code blocks
    code_blocks = _parse_code_blocks(text)

    # If message fits, return as-is
    if len(text) <= max_length:
        return [text]

    chunks = []
    current_pos = 0
    in_continued_block = None  # Track if we're continuing a code block

    while current_pos < len(text):
        remaining = text[current_pos:]

        # Check if we can fit the rest
        if len(remaining) <= SAFE_LIMIT:
            # If we're continuing a code block, prepend opening tag
            if in_continued_block:
                remaining = f"```{in_continued_block['language']}\n{remaining}"
            chunks.append(remaining)
            break

        # Find optimal split point
        split_info = _find_split_point(text, current_pos, current_pos + SAFE_LIMIT, code_blocks)
        split_pos = split_info["position"]

        # Extract chunk
        chunk = text[current_pos:split_pos]

        # If we're continuing a code block from previous chunk, prepend opening tag
        if in_continued_block:
            chunk = f"```{in_continued_block['language']}\n{chunk}"

        # If we're splitting inside a code block, close it
        if split_info["in_code_block"]:
            chunk += '\n```'
            in_continued_block = split_info["block_info"]
        else:
            in_continued_block = None

        chunks.append(chunk)
        current_pos = split_pos

    # Add continuation markers
    if len(chunks) > 1:
        total = len(chunks)
        chunks = [f"({i + 1}/{total}) {chunk}" for i, chunk in enumerate(chunks)]

    return chunks


def pane_exists(pane: str) -> bool:
    """Check if a tmux pane exists."""
    result = subprocess.run(
        ["tmux", "has-session", "-t", pane],
        capture_output=True
    )
    return result.returncode == 0


def send_to_tmux_pane(pane: str, text: str) -> bool:
    """Send text to a tmux pane. Clears line first, sends text, then Enter.

    Returns True on success, False on failure (e.g., pane dead).
    """
    try:
        subprocess.run(["tmux", "send-keys", "-t", pane, "C-u"], check=True)
        subprocess.run(["tmux", "send-keys", "-t", pane, "-l", text], check=True)
        # Longer text needs more time for TUI to process before Enter
        delay = 0.1 + len(text) / 10000  # ~0.1s base + 0.1s per 1000 chars
        time.sleep(delay)
        subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], check=True)
        return True
    except subprocess.CalledProcessError:
        return False


def _send_single_telegram(bot_token: str, chat_id: str, msg: str, parse_mode: str, reply_markup: dict = None) -> dict | None:
    """Send a single message to Telegram (internal helper). Returns response JSON on success."""
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


def send_telegram(bot_token: str, chat_id: str, msg: str, tool_name: str = None, reply_markup: dict = None, parse_mode: str = "Markdown") -> dict | None:
    """Send message to Telegram. Returns response JSON on success.

    Automatically splits messages exceeding 4096 characters into multiple chunks.
    Code blocks are properly closed and reopened across splits.
    Buttons (reply_markup) appear only on the last chunk.

    parse_mode: "Markdown", "MarkdownV2", or "HTML"
    For MarkdownV2, caller must escape text pieces before assembly (use escape_markdown_v2).
    """
    # Split message if needed
    chunks = split_message_with_code_blocks(msg)

    # Send all chunks except last without buttons
    for chunk in chunks[:-1]:
        _send_single_telegram(bot_token, chat_id, chunk, parse_mode, None)

    # Send last chunk with reply_markup (buttons)
    return _send_single_telegram(bot_token, chat_id, chunks[-1], parse_mode, reply_markup)


def answer_callback(bot_token: str, callback_id: str, text: str = None):
    """Answer a callback query to dismiss the loading state."""
    requests.post(
        f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery",
        json={"callback_query_id": callback_id, "text": text}
    )


def send_reply(bot_token: str, chat_id: str, reply_to_msg_id: int, text: str, parse_mode: str = None):
    """Send a reply message on Telegram.

    Automatically splits messages exceeding 4096 characters into multiple chunks.
    Only the first chunk maintains the reply reference; subsequent chunks are sent as regular messages.
    """
    # Split message if needed
    chunks = split_message_with_code_blocks(text)

    # Send first chunk with reply reference
    payload = {"chat_id": chat_id, "text": chunks[0], "reply_to_message_id": reply_to_msg_id}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    requests.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        json=payload
    )

    # Send remaining chunks as regular messages
    for chunk in chunks[1:]:
        _send_single_telegram(bot_token, chat_id, chunk, parse_mode or "Markdown", None)


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


def register_bot_commands(bot_token: str):
    """Register bot commands with Telegram. Raises on failure."""
    commands = [
        {"command": "dump", "description": "Dump tmux pane output"},
        {"command": "debug", "description": "Show debug info for a message (reply to it)"},
        {"command": "show_tmux_command", "description": "Show tmux attach command"},
        {"command": "spawn", "description": "Create a new task"},
        {"command": "status", "description": "Show all tasks and status"},
        {"command": "cleanup", "description": "Clean up a task"},
        {"command": "help", "description": "Show available commands"},
        {"command": "todo", "description": "Add todo to Operator queue"},
        {"command": "setup", "description": "Initialize this group as control center"},
        {"command": "summarize", "description": "Have operator summarize all tasks"},
        {"command": "operator", "description": "Request operator intervention for task"},
        {"command": "rebuild_registry", "description": "Rebuild registry from markers (maintenance)"},
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
    """Close (archive) a forum topic. Returns True on success."""
    resp = requests.post(
        f"https://api.telegram.org/bot{bot_token}/closeForumTopic",
        json={"chat_id": chat_id, "message_thread_id": topic_id}
    )
    return resp.ok


def delete_forum_topic(bot_token: str, chat_id: str, topic_id: int) -> bool:
    """Delete a forum topic permanently. Returns True on success."""
    resp = requests.post(
        f"https://api.telegram.org/bot{bot_token}/deleteForumTopic",
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


def _send_single_to_topic(bot_token: str, chat_id: str, topic_id: int, text: str,
                          parse_mode: str, reply_markup: dict = None) -> dict | None:
    """Send a single message to a forum topic (internal helper). Returns response JSON on success."""
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


def send_to_topic(bot_token: str, chat_id: str, topic_id: int, text: str,
                  reply_markup: dict = None, parse_mode: str = "MarkdownV2") -> dict | None:
    """Send message to a specific forum topic. Returns response JSON on success.

    Automatically splits messages exceeding 4096 characters into multiple chunks.
    Code blocks are properly closed and reopened across splits.
    Buttons (reply_markup) appear only on the last chunk.

    For the General topic (topic_id=1), don't pass message_thread_id as Telegram
    expects messages to the General topic to be sent without it.
    """
    # Split message if needed
    chunks = split_message_with_code_blocks(text)

    # Send all chunks except last without buttons
    for chunk in chunks[:-1]:
        _send_single_to_topic(bot_token, chat_id, topic_id, chunk, parse_mode, None)

    # Send last chunk with reply_markup (buttons)
    return _send_single_to_topic(bot_token, chat_id, topic_id, chunks[-1], parse_mode, reply_markup)


def get_chat_administrators(bot_token: str, chat_id: str) -> list | None:
    """Get list of chat administrators. Returns None on error."""
    resp = requests.post(
        f"https://api.telegram.org/bot{bot_token}/getChatAdministrators",
        json={"chat_id": chat_id}
    )
    if not resp.ok:
        return None
    return resp.json().get("result", [])
