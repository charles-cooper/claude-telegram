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


def read_state() -> dict:
    """Read state file."""
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except:
        return {}


def write_state(state: dict):
    """Write state file."""
    STATE_FILE.write_text(json.dumps(state))


def strip_home(path: str) -> str:
    """Remove home directory prefix from path."""
    return path.removeprefix(str(Path.home()) + "/")


def escape_markdown(text: str) -> str:
    """Escape Telegram markdown special characters in plain text.

    Triple backticks are escaped to prevent code block issues.
    Single backticks are left alone.
    """
    text = text.replace("```", "\\`\\`\\`")
    for char in ['_', '*', '[', ']']:
        text = text.replace(char, '\\' + char)
    return text


def format_tool_permission(tool_name: str, tool_input: dict) -> str:
    """Format a tool call for display."""
    if tool_name == "Bash":
        cmd = tool_input.get("command", "").replace("```", "'''")
        desc = tool_input.get("description", "")
        desc_escaped = escape_markdown(desc).replace("\\_", "_")
        desc_line = f"\n\n_{desc_escaped}_" if desc else ""
        return f"Claude is asking permission to run:\n\n```bash\n{cmd}\n```{desc_line}"

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
        return f"Claude is asking permission to edit `{fp}`:\n\n```diff\n{diff}\n```"

    elif tool_name == "Write":
        fp = strip_home(tool_input.get("file_path", ""))
        content = tool_input.get("content", "").replace("```", "'''")
        return f"Claude is asking permission to write `{fp}`:\n\n```\n{content}\n```"

    elif tool_name == "Read":
        fp = strip_home(tool_input.get("file_path", ""))
        return f"Claude is asking permission to read `{fp}`"

    elif tool_name == "AskUserQuestion":
        questions = tool_input.get("questions", [])
        lines = ["Claude is asking:\n"]
        for q in questions:
            question_text = escape_markdown(q.get('question', '')).replace("\\_", "_").replace("\\*", "*")
            lines.append(f"*{question_text}*\n")
            for opt in q.get("options", []):
                label = escape_markdown(opt.get('label', ''))
                lines.append(f"• {label}")
        return "\n".join(lines)

    else:
        input_str = json.dumps(tool_input, indent=2).replace("```", "'''")
        return f"Claude is asking permission to use {tool_name}:\n\n```\n{input_str}\n```"


def get_tmux_pane() -> str | None:
    """Get current tmux pane identifier."""
    pane = os.environ.get("TMUX_PANE")
    if pane:
        try:
            result = subprocess.run(
                ["tmux", "display-message", "-p", "#{session_name}:#{window_index}.#{pane_index}"],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except:
            pass
    return None


def pane_exists(pane: str) -> bool:
    """Check if a tmux pane exists."""
    result = subprocess.run(
        ["tmux", "has-session", "-t", pane],
        capture_output=True
    )
    return result.returncode == 0


def send_telegram(bot_token: str, chat_id: str, msg: str, tool_name: str = None, reply_markup: dict = None) -> dict | None:
    """Send message to Telegram. Returns response JSON on success."""
    payload = {"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"}

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
