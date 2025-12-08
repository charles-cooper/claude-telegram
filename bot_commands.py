"""Bot command handlers for Telegram integration."""

import datetime
import json
import time

from telegram_utils import (
    State, pane_exists, send_reply, react_to_message, log,
    is_forum_enabled
)
from registry import get_config, get_registry, rebuild_registry_from_markers
from session_operator import (
    start_operator_session, stop_operator_session, send_to_operator
)


class CommandHandler:
    """Handles bot commands like /debug, /todo, /setup."""

    def __init__(self, bot_token: str, chat_id: str, state: State):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.state = state

    def _reply(self, msg_id: int, text: str):
        """Send a reply to a message."""
        send_reply(self.bot_token, self.chat_id, msg_id, text)

    def _react(self, msg_id: int, emoji: str = "👀"):
        """React to a message."""
        react_to_message(self.bot_token, self.chat_id, msg_id, emoji)

    def _format_reply_context(self, msg: dict) -> str | None:
        """Format context from a replied-to message for the operator."""
        reply_to = msg.get("reply_to_message")
        if not reply_to:
            return None

        reply_msg_id = reply_to.get("message_id")
        reply_text = reply_to.get("text", "")[:500]  # Truncate long messages
        reply_from = reply_to.get("from", {}).get("first_name", "Unknown")
        reply_date = reply_to.get("date", 0)

        # Check if we have state info for this message
        state_info = ""
        reply_str = str(reply_msg_id)
        if reply_str in self.state:
            entry = self.state.get(reply_str)
            state_info = f"\nState: type={entry.get('type')}, pane={entry.get('pane')}"

        ts = datetime.datetime.fromtimestamp(reply_date).strftime("%H:%M:%S") if reply_date else "?"
        return f"[Replying to msg_id={reply_msg_id} from {reply_from} at {ts}]\n{reply_text}{state_info}"

    def handle_command(self, msg: dict) -> bool:
        """Handle a command message. Returns True if handled, False otherwise."""
        text = msg.get("text", "").strip()
        msg_id = msg.get("message_id")
        topic_id = msg.get("message_thread_id")
        text_lower = text.lower()

        # /todo - route to operator
        if text_lower.startswith("/todo"):
            self._handle_todo(msg, msg_id, text, topic_id)
            return True

        # /debug - route to operator (requires reply)
        if text_lower.startswith("/debug") or (msg.get("reply_to_message") and text_lower in ("debug", "?")):
            self._handle_debug(msg, msg_id, text)
            return True

        # /setup - initialize group
        if text_lower.startswith("/setup"):
            self._handle_setup(msg, msg_id)
            return True

        # /reset - remove configuration
        if text_lower.startswith("/reset"):
            self._handle_reset(msg, msg_id)
            return True

        # /help - show commands
        if text_lower.startswith("/help"):
            self._handle_help(msg_id)
            return True

        # /status - show task status
        if text_lower.startswith("/status"):
            self._handle_status(msg_id)
            return True

        # /recover - rebuild registry from marker files
        if text_lower.startswith("/recover"):
            self._handle_recover(msg_id)
            return True

        return False

    def _handle_todo(self, msg: dict, msg_id: int, text: str, topic_id: int | None):
        """Handle /todo - send to operator with context."""
        todo_text = text[5:].strip()  # Remove "/todo"
        if not todo_text:
            self._reply(msg_id, "Usage: /todo <item>")
            return

        # Build message for operator
        lines = ["[TODO]"]
        if topic_id:
            lines.append(f"From topic: {topic_id}")

        # Include reply context if present
        reply_ctx = self._format_reply_context(msg)
        if reply_ctx:
            lines.append(reply_ctx)

        lines.append(f"Item: {todo_text}")

        if send_to_operator("\n".join(lines)):
            self._react(msg_id)
            log(f"  /todo sent to operator: {todo_text[:50]}...")
        else:
            self._reply(msg_id, "Operator not available")

    def _handle_debug(self, msg: dict, msg_id: int, text: str):
        """Handle /debug - send debug request to operator."""
        if not msg.get("reply_to_message"):
            self._reply(msg_id, "Reply to a message to debug it")
            return

        # Extract note after /debug
        user_note = ""
        text_lower = text.lower()
        if text_lower.startswith("/debug"):
            user_note = text[6:].strip()
        elif text_lower.startswith("debug"):
            user_note = text[5:].strip()

        reply_to_id = msg["reply_to_message"]["message_id"]

        # Build debug request for operator
        lines = [f"[DEBUG] msg_id={reply_to_id}"]
        if user_note:
            lines.append(f"User note: {user_note}")

        # Include full reply context
        reply_ctx = self._format_reply_context(msg)
        if reply_ctx:
            lines.append(reply_ctx)

        # Add state info if available
        reply_str = str(reply_to_id)
        if reply_str in self.state:
            entry = self.state.get(reply_str)
            lines.append(f"Full state: {json.dumps(entry)}")

        if send_to_operator("\n".join(lines)):
            self._react(msg_id)
            log(f"  /debug sent to operator for msg_id={reply_to_id}")
        else:
            self._reply(msg_id, "Operator not available")

    def _handle_setup(self, msg: dict, msg_id: int):
        """Handle /setup - initialize group as Claude Army control center."""
        chat = msg.get("chat", {})
        chat_id = chat.get("id")
        chat_type = chat.get("type")

        log(f"  /setup in chat {chat_id} (type: {chat_type})")

        if chat_type not in ("group", "supergroup"):
            self._reply(msg_id, "This command only works in group chats.")
            return

        config = get_config()

        if config.is_configured():
            if config.group_id != chat_id:
                self._reply(msg_id,
                    f"Already configured for another group (ID: {config.group_id}). "
                    "Run /reset in that group first.")
                return
            self._reply(msg_id, "Already set up in this group.")
            return

        if not is_forum_enabled(self.bot_token, str(chat_id)):
            self._reply(msg_id,
                "This group needs to be a Forum (supergroup with topics enabled).\n\n"
                "To enable:\n"
                "1. Open group settings\n"
                "2. Go to 'Topics'\n"
                "3. Enable topics\n\n"
                "Then run /setup again.")
            return

        # Store configuration
        config.group_id = chat_id
        config.general_topic_id = 1  # General topic in forums

        # Start operator session
        pane = start_operator_session()
        if pane:
            self._reply(msg_id,
                "Claude Army initialized!\n\n"
                "Operator Claude is running. Send messages here to interact.\n\n"
                "Use /help to see available commands.")
        else:
            self._reply(msg_id,
                "Claude Army configured, but failed to start Operator session.\n"
                "Check tmux availability.")

        log(f"  Setup complete for group {chat_id}")

    def _handle_reset(self, msg: dict, msg_id: int):
        """Handle /reset - remove Claude Army configuration."""
        chat = msg.get("chat", {})
        chat_id = chat.get("id")

        log(f"  /reset in chat {chat_id}")

        config = get_config()

        if not config.is_configured():
            self._reply(msg_id, "Claude Army is not configured.")
            return

        if config.group_id != chat_id:
            self._reply(msg_id,
                "Claude Army is configured for a different group. "
                "Run /reset in that group.")
            return

        # Stop operator and clear config
        stop_operator_session()
        config.clear()
        self._reply(msg_id,
            "Claude Army configuration cleared. "
            "You can run /setup in any group to reconfigure.")
        log(f"  Reset complete for group {chat_id}")

    def _handle_status(self, msg_id: int):
        """Handle /status - show all tasks and their status."""
        config = get_config()
        registry = get_registry()

        if not config.is_configured():
            self._reply(msg_id, "Claude Army not configured. Run /setup first.")
            return

        tasks = registry.get_all_tasks()
        if not tasks:
            self._reply(msg_id, "No active tasks.")
            return

        lines = ["*Task Status*\n"]
        for repo_path, task_name, task_data in tasks:
            status = task_data.get("status", "unknown")
            topic_id = task_data.get("topic_id", "?")
            emoji = "▶️" if status == "active" else "⏸️" if status == "paused" else "❓"
            lines.append(f"{emoji} `{task_name}` ({status}) - topic {topic_id}")

        self._reply(msg_id, "\n".join(lines))

    def _handle_recover(self, msg_id: int):
        """Handle /recover - rebuild registry from marker files."""
        config = get_config()

        if not config.is_configured():
            self._reply(msg_id, "Claude Army not configured. Run /setup first.")
            return

        self._reply(msg_id, "Scanning for marker files...")
        recovered = rebuild_registry_from_markers()

        if recovered > 0:
            self._reply(msg_id, f"Recovered {recovered} task(s). Run /status to see them.")
        else:
            self._reply(msg_id, "No new tasks found.")

    def _handle_help(self, msg_id: int):
        """Handle /help - show available commands."""
        config = get_config()

        help_text = """*Claude Army Commands*

/setup - Initialize this group as control center
/reset - Remove Claude Army configuration
/status - Show all tasks and status
/recover - Rebuild registry from marker files
/help - Show this help message
/todo <item> - Add todo to Operator queue
/debug - Debug a message (reply to it)

*Operator Commands* (natural language):
• "Create task X in repo Y"
• "What's the status?"
• "Pause/resume task X"
• "Clean up task X"
"""
        if config.is_configured():
            help_text += f"\n_Status: Configured (group {config.group_id})_"
        else:
            help_text += "\n_Status: Not configured_"

        self._reply(msg_id, help_text)
