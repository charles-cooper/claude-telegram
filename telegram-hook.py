#!/usr/bin/env python3
"""Claude Code hook - sends Telegram notifications."""

import datetime
import difflib
import fcntl
import json
import os
import requests
import subprocess
import sys
from pathlib import Path

CONFIG_FILE = Path.home() / "telegram.json"
STATE_FILE = Path("/tmp/claude-telegram-state.json")
LOCK_FILE = Path("/tmp/claude-telegram-state.lock")
LOG_FILE = Path("/tmp/claude-telegram-hook.log")


def log(msg: str):
    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    with open(LOG_FILE, "a") as f:
        f.write(f"[{ts}] {msg}\n")


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


def strip_home(path: str) -> str:
    """Remove home directory prefix from path."""
    return path.removeprefix(str(Path.home()) + "/")


def escape_markdown(text: str) -> str:
    """Escape Telegram markdown special characters in plain text."""
    for char in ['_', '*', '`', '[', ']']:
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
        return f"Claude is asking permission to write `{fp}`"

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


def extract_tool_from_transcript(transcript_path: str) -> tuple[dict | None, str]:
    """Extract pending tool call and assistant text from transcript.

    Returns (tool_call, assistant_text) tuple.
    Only returns text from the same message as the tool_call.
    """
    lines = Path(transcript_path).read_text().strip().split("\n")

    for line in reversed(lines):
        entry = json.loads(line)
        if entry.get("type") != "assistant":
            continue

        content = entry.get("message", {}).get("content", [])
        tool_call = None
        assistant_text = ""

        for c in content:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "tool_use":
                tool_call = c
            elif c.get("type") == "text":
                assistant_text = c.get("text", "")

        if tool_call:
            return tool_call, assistant_text

    return None, ""


def extract_last_assistant_text(transcript_path: str) -> str:
    """Extract last assistant text from transcript."""
    for line in reversed(Path(transcript_path).read_text().strip().split("\n")):
        entry = json.loads(line)
        if entry.get("type") != "assistant":
            continue
        for c in entry.get("message", {}).get("content", []):
            if isinstance(c, dict) and c.get("type") == "text":
                return c.get("text", "")
    return ""


def build_context(hook_input: dict) -> str:
    """Build notification context from hook input."""
    hook_event = hook_input.get("hook_event_name", "")
    notification_type = hook_input.get("notification_type", "")
    transcript_path = hook_input.get("transcript_path", "")

    if hook_event == "PreCompact":
        trigger = hook_input.get("trigger", "auto")
        return f"🔄 Compacting context ({trigger})..."

    if notification_type == "permission_prompt":
        try:
            tool_call, assistant_text = extract_tool_from_transcript(transcript_path)
            if tool_call:
                prefix = f"{escape_markdown(assistant_text)}\n\n---\n\n" if assistant_text else ""
                tool_desc = format_tool_permission(tool_call.get("name", ""), tool_call.get("input", {}))
                return f"{prefix}{tool_desc}"
        except:
            pass
        return hook_input.get("message", "")

    if "message" in hook_input:
        return hook_input.get("message", "")

    try:
        return escape_markdown(extract_last_assistant_text(transcript_path))
    except:
        return ""


def send_telegram(bot_token: str, chat_id: str, msg: str, notification_type: str) -> dict | None:
    """Send message to Telegram. Returns response JSON on success."""
    payload = {"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"}

    if notification_type == "permission_prompt":
        payload["reply_markup"] = {
            "inline_keyboard": [[
                {"text": "✓ Allow", "callback_data": "y"},
                {"text": "✗ Deny", "callback_data": "n"}
            ]]
        }

    log("Sending to Telegram...")
    resp = requests.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        json=payload
    )

    if not resp.ok:
        log(f"Telegram error: {resp.status_code} {resp.text}")
        return None

    log("Sent OK")
    return resp.json()


def save_message_state(msg_id: int, session_id: str, cwd: str, notification_type: str):
    """Save message to state for reply tracking."""
    state = read_state()
    entry = {"session_id": session_id, "cwd": cwd}
    pane = get_tmux_pane()
    if pane:
        entry["pane"] = pane
    if notification_type:
        entry["type"] = notification_type
    state[str(msg_id)] = entry
    write_state(state)


def main():
    hook_input = json.load(sys.stdin)
    log(f"Hook called: {json.dumps(hook_input)}")

    config = json.loads(CONFIG_FILE.read_text())
    bot_token, chat_id = config["bot_token"], config["chat_id"]

    cwd = hook_input.get("cwd", "")
    session_id = hook_input.get("session_id", "")
    notification_type = hook_input.get("notification_type", "")
    project = strip_home(cwd)

    context = build_context(hook_input)
    msg = f"`{project}`\n\n{context}" if context else f"`{project}`"

    result = send_telegram(bot_token, chat_id, msg, notification_type)
    if result:
        msg_id = result.get("result", {}).get("message_id")
        if msg_id:
            save_message_state(msg_id, session_id, cwd, notification_type)


if __name__ == "__main__":
    main()
