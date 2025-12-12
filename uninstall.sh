#!/bin/bash
# Uninstall Claude Code Telegram notifications (Legacy Hook Cleanup)
#
# NOTE: The modern daemon-based architecture does NOT use hooks.
# This script is only needed if you previously installed an older version
# that configured hooks in ~/.claude/settings.json.
#
# Removes only our old telegram-hook.py hooks from ~/.claude/settings.json,
# preserving other hooks.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SETTINGS_FILE="$HOME/.claude/settings.json"
HOOK_CMD="python3 $SCRIPT_DIR/telegram-hook.py"

echo "Claude Code Telegram - Legacy Hook Cleanup"
echo "==========================================="
echo
echo "Removing old telegram-hook.py references from $SETTINGS_FILE..."

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

# Remove from PostCompact
if "PostCompact" in hooks:
    hooks["PostCompact"] = [h for h in hooks["PostCompact"]
                            if h.get("hooks", [{}])[0].get("command") != "$HOOK_CMD"]
    if not hooks["PostCompact"]:
        del hooks["PostCompact"]

if not hooks:
    del settings["hooks"]

settings_file.write_text(json.dumps(settings, indent=2))
print("Done.")
EOF
else
    echo "No settings file found - nothing to clean up."
fi

echo
echo "==========================================="
echo "Done! Old hook configurations have been removed."
echo
echo "Note: Config file ~/telegram.json was NOT removed."
echo "The modern daemon (telegram-daemon.py) does not use hooks."
