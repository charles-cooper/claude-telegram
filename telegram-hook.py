#!/usr/bin/env python3
"""Claude Code hook - sends Telegram notifications."""

import difflib
import json
import sys
from pathlib import Path

CONFIG_FILE = Path.home() / "telegram.json"
STATE_FILE = Path.home() / ".claude-telegram-state.json"


LOG_FILE = Path("/tmp/claude-telegram-hook.log")


def log(msg: str):
    with open(LOG_FILE, "a") as f:
        f.write(f"{msg}\n")


def main():
    hook_input = json.load(sys.stdin)
    log(f"Hook called: {json.dumps(hook_input)}")

    config = json.loads(CONFIG_FILE.read_text())
    bot_token, chat_id = config["bot_token"], config["chat_id"]

    cwd = hook_input.get("cwd", "")
    session_id = hook_input.get("session_id", "")

    # Get project path (strip home directory)
    project = cwd.removeprefix(str(Path.home()) + "/")

    # Check if this is a Notification event (has message field) or Stop event (has transcript_path)
    context = ""
    notification_type = hook_input.get("notification_type", "")
    transcript_path = hook_input.get("transcript_path", "")

    if notification_type == "permission_prompt":
        # Permission prompt - extract pending tool call from transcript
        try:
            for line in reversed(Path(transcript_path).read_text().strip().split("\n")):
                entry = json.loads(line)
                if entry.get("type") == "assistant":
                    for c in entry.get("message", {}).get("content", []):
                        if isinstance(c, dict) and c.get("type") == "tool_use":
                            tool_name = c.get("name", "")
                            tool_input = c.get("input", {})
                            if tool_name == "Bash":
                                cmd = tool_input.get("command", "")
                                desc = tool_input.get("description", "")
                                desc_line = f"\n\n_{desc}_" if desc else ""
                                context = f"Claude is asking permission to run:\n\n`{cmd}`{desc_line}"
                            elif tool_name == "Edit":
                                fp = tool_input.get("file_path", "").removeprefix(str(Path.home()) + "/")
                                old = tool_input.get("old_string", "")
                                new = tool_input.get("new_string", "")
                                diff = "\n".join(
                                    line.rstrip() for line in difflib.unified_diff(
                                        old.splitlines(),
                                        new.splitlines(),
                                        fromfile=fp,
                                        tofile=fp,
                                        n=9999
                                    )
                                )
                                context = f"Claude is asking permission to edit `{fp}`:\n\n```diff\n{diff}\n```"
                            elif tool_name == "Write":
                                fp = tool_input.get("file_path", "").removeprefix(str(Path.home()) + "/")
                                context = f"Claude is asking permission to write `{fp}`"
                            elif tool_name == "Read":
                                fp = tool_input.get("file_path", "").removeprefix(str(Path.home()) + "/")
                                context = f"Claude is asking permission to read `{fp}`"
                            else:
                                # Show tool input for unknown tools
                                input_str = json.dumps(tool_input, indent=2)
                                context = f"Claude is asking permission to use {tool_name}:\n\n```\n{input_str}\n```"
                            break
                    break
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
    import requests
    msg = f"`{project}`\n\n{context}" if context else f"`{project}`"
    resp = requests.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"}
    )

    if resp.ok:
        msg_id = resp.json().get("result", {}).get("message_id")
        if msg_id:
            state = {}
            if STATE_FILE.exists():
                try:
                    state = json.loads(STATE_FILE.read_text())
                except:
                    pass
            state[str(msg_id)] = {"session_id": session_id, "cwd": cwd}
            STATE_FILE.write_text(json.dumps(state))


if __name__ == "__main__":
    main()
