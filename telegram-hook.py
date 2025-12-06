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
    # Try TMUX_PANE env var first
    pane = os.environ.get("TMUX_PANE")
    if pane:
        # Convert %N format to session:window.pane format
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


LOG_FILE = Path("/tmp/claude-telegram-hook.log")


def log(msg: str):
    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    with open(LOG_FILE, "a") as f:
        f.write(f"[{ts}] {msg}\n")


def strip_home(path: str) -> str:
    """Remove home directory prefix from path."""
    return path.removeprefix(str(Path.home()) + "/")


def format_tool_permission(tool_name: str, tool_input: dict) -> str:
    """Format a tool call for display."""
    if tool_name == "Bash":
        cmd = tool_input.get("command", "").replace("```", "'''")
        desc = tool_input.get("description", "")
        desc_line = f"\n\n_{desc}_" if desc else ""
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
        diff = diff.replace("```", "'''")  # Escape inner code fences
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
            lines.append(f"*{q.get('question', '')}*\n")
            for opt in q.get("options", []):
                lines.append(f"• {opt.get('label', '')}")
        return "\n".join(lines)

    else:
        input_str = json.dumps(tool_input, indent=2)
        return f"Claude is asking permission to use {tool_name}:\n\n```\n{input_str}\n```"


def main():
    hook_input = json.load(sys.stdin)
    log(f"Hook called: {json.dumps(hook_input)}")

    config = json.loads(CONFIG_FILE.read_text())
    bot_token, chat_id = config["bot_token"], config["chat_id"]

    cwd = hook_input.get("cwd", "")
    session_id = hook_input.get("session_id", "")

    # Get project path (strip home directory)
    project = cwd.removeprefix(str(Path.home()) + "/")

    # Check event type
    context = ""
    notification_type = hook_input.get("notification_type", "")
    hook_event = hook_input.get("hook_event_name", "")
    transcript_path = hook_input.get("transcript_path", "")

    # Handle PreCompact event
    if hook_event == "PreCompact":
        trigger = hook_input.get("trigger", "auto")
        context = f"🔄 Compacting context ({trigger})..."

    elif notification_type == "permission_prompt":
        # Permission prompt - extract pending tool call from transcript
        try:
            lines = Path(transcript_path).read_text().strip().split("\n")
            assistant_text = ""
            tool_call = None

            # Find the tool_use and preceding text (may be in separate messages)
            for line in reversed(lines):
                entry = json.loads(line)
                if entry.get("type") == "assistant":
                    content = entry.get("message", {}).get("content", [])

                    for c in content:
                        if isinstance(c, dict):
                            if c.get("type") == "tool_use" and not tool_call:
                                tool_call = c
                            elif c.get("type") == "text" and not assistant_text:
                                assistant_text = c.get("text", "")

                    # Stop once we have both
                    if tool_call and assistant_text:
                        break

            if tool_call:
                prefix = f"{assistant_text}\n\n---\n\n" if assistant_text else ""
                tool_desc = format_tool_permission(tool_call.get("name", ""), tool_call.get("input", {}))
                context = f"{prefix}{tool_desc}"
        except:
            context = hook_input.get("message", "")
    elif "message" in hook_input:
        # Other notification event - use the message directly
        context = hook_input.get("message", "")
    else:
        # Stop event - extract from transcript
        try:
            for line in reversed(Path(transcript_path).read_text().strip().split("\n")):
                entry = json.loads(line)
                if entry.get("type") == "assistant":
                    for c in entry.get("message", {}).get("content", []):
                        if isinstance(c, dict) and c.get("type") == "text":
                            context = c.get("text", "")
                            break
                    break
        except:
            pass

    # Send telegram
    msg = f"`{project}`\n\n{context}" if context else f"`{project}`"
    payload = {"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"}

    # Add buttons for permission prompts
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
    else:
        log("Sent OK")

    if resp.ok:
        msg_id = resp.json().get("result", {}).get("message_id")
        if msg_id:
            state = read_state()
            entry = {"session_id": session_id, "cwd": cwd}
            pane = get_tmux_pane()
            if pane:
                entry["pane"] = pane
            if notification_type:
                entry["type"] = notification_type
            state[str(msg_id)] = entry
            write_state(state)


if __name__ == "__main__":
    main()
