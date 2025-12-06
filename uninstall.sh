#!/bin/bash
# Uninstall Claude Code Telegram notifications
#
# Removes only our hooks from ~/.claude/settings.json, preserving other hooks.
#
# Test cases:
#
# 1. Only our hooks -> hooks section removed entirely:
#    {"hooks": {"Notification": [our_hook], "PreCompact": [our_hook]}}
#    becomes: {}
#
# 2. Mixed with other hooks -> only ours removed:
#    {"hooks": {"Notification": [our_hook, other_hook]}}
#    becomes: {"hooks": {"Notification": [other_hook]}}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SETTINGS_FILE="$HOME/.claude/settings.json"
HOOK_CMD="python3 $SCRIPT_DIR/telegram-hook.py"

echo "Removing hooks from $SETTINGS_FILE..."

if [ -f "$SETTINGS_FILE" ]; then
    python3 << EOF
import json
from pathlib import Path

settings_file = Path("$SETTINGS_FILE")
settings = json.loads(settings_file.read_text())
hooks = settings.get("hooks", {})

# Remove from Notification
if "Notification" in hooks:
    hooks["Notification"] = [h for h in hooks["Notification"]
                             if h.get("hooks", [{}])[0].get("command") != "$HOOK_CMD"]
    if not hooks["Notification"]:
        del hooks["Notification"]

# Remove from PreCompact
if "PreCompact" in hooks:
    hooks["PreCompact"] = [h for h in hooks["PreCompact"]
                           if h.get("hooks", [{}])[0].get("command") != "$HOOK_CMD"]
    if not hooks["PreCompact"]:
        del hooks["PreCompact"]

if not hooks:
    del settings["hooks"]

settings_file.write_text(json.dumps(settings, indent=2))
print("Done.")
EOF
else
    echo "No settings file found."
fi

echo
echo "Hooks removed. Config file ~/telegram.json was NOT removed."
