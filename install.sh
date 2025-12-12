#!/bin/bash
# Install Claude Code Telegram notifications
#
# Merges hooks into ~/.claude/settings.json, preserving existing hooks.
# Safe to run multiple times - won't duplicate hooks.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_FILE="$HOME/telegram.json"
SETTINGS_FILE="$HOME/.claude/settings.json"

echo "Claude Code Telegram Notifications - Install"
echo "============================================="
echo

# Check for requests
if ! python3 -c "import requests" 2>/dev/null; then
    echo "Installing requests..."
    pip3 install --user requests
fi

# Configure telegram bot
if [ -f "$CONFIG_FILE" ]; then
    echo "Telegram config already exists at $CONFIG_FILE"
    read -p "Overwrite? [y/N] " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Keeping existing config."
    else
        rm "$CONFIG_FILE"
    fi
fi

if [ ! -f "$CONFIG_FILE" ]; then
    echo "To get a bot token:"
    echo "  1. Message @BotFather on Telegram"
    echo "  2. Send /newbot and follow prompts"
    echo "  3. Copy the token"
    echo
    read -p "Bot token: " BOT_TOKEN

    echo
    echo "To get your chat ID:"
    echo "  1. Message your bot"
    echo "  2. Visit: https://api.telegram.org/bot<TOKEN>/getUpdates"
    echo "  3. Find 'chat':{'id': NUMBER}"
    echo
    read -p "Chat ID: " CHAT_ID

    echo "{\"bot_token\": \"$BOT_TOKEN\", \"chat_id\": \"$CHAT_ID\"}" > "$CONFIG_FILE"
    echo "Saved to $CONFIG_FILE"
fi

echo
echo "============================================="
echo "Installation complete!"
echo
echo "To start receiving Telegram notifications, run the daemon:"
echo
echo "  ./telegram-daemon.py"
echo
echo "For background operation:"
echo
echo "  nohup ./telegram-daemon.py > /tmp/telegram-daemon.log 2>&1 &"
echo
