"""Bot command handlers for Telegram integration."""

import datetime
import json
import time

from telegram_utils import (
    State, pane_exists, send_reply, react_to_message, log,
    is_forum_enabled, get_chat
)
from registry import get_config, get_registry


def send_to_pane(pane: str, text: str) -> bool:
    """Send text to a tmux pane."""
    import subprocess
    try:
        subprocess.run(["tmux", "send-keys", "-t", pane, "C-u"], check=True)
        subprocess.run(["tmux", "send-keys", "-t", pane, "-l", text], check=True)
        time.sleep(0.1)
        subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], check=True)
        return True
    except subprocess.CalledProcessError as e:
        log(f"  Error: {e}")
        return False


def tool_already_handled(transcript_path: str, tool_use_id: str) -> bool:
    """Check if a tool_use has a corresponding tool_result in the transcript."""
    if not transcript_path or not tool_use_id:
        return False
    try:
        with open(transcript_path) as f:
            for line in f:
                if tool_use_id in line and '"tool_result"' in line:
                    return True
    except Exception as e:
        log(f"  Error checking transcript: {e}")
    return False


class CommandHandler:
    """Handles bot commands like /debug, /todo."""

    def __init__(self, bot_token: str, chat_id: str, state: State):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.state = state

    def handle_command(self, msg: dict) -> bool:
        """Handle a command message. Returns True if handled, False otherwise."""
        text = msg.get("text", "").strip()
        msg_id = msg.get("message_id")
        reply_to = msg.get("reply_to_message", {}).get("message_id")
        text_lower = text.lower()

        # /debug - requires reply to a message
        if reply_to and (text_lower.startswith("/debug") or text_lower in ("debug", "?")):
            # Extract any additional text after /debug
            user_note = ""
            if text_lower.startswith("/debug"):
                user_note = text[6:].strip()
            elif text_lower.startswith("debug"):
                user_note = text[5:].strip()
            self._handle_debug(msg_id, reply_to, user_note)
            return True

        # /todo <item> - add todo to Claude's stack
        if text_lower.startswith("/todo"):
            self._handle_todo(msg_id, text)
            return True

        # /setup - initialize group as Claude Army control center
        if text_lower.startswith("/setup"):
            self._handle_setup(msg, msg_id)
            return True

        # /reset - remove Claude Army configuration
        if text_lower.startswith("/reset"):
            self._handle_reset(msg, msg_id)
            return True

        # /help - show available commands
        if text_lower.startswith("/help"):
            self._handle_help(msg_id)
            return True

        return False

    def _get_active_pane(self) -> str | None:
        """Get the most recently active pane from state."""
        latest_time = 0
        latest_pane = None
        for _, entry in self.state.items():
            notified_at = entry.get("notified_at", 0)
            pane = entry.get("pane")
            if pane and notified_at > latest_time:
                latest_time = notified_at
                latest_pane = pane
        return latest_pane

    def _search_logs_for_msg(self, msg_id: int) -> list[str] | None:
        """Search daemon logs for entries about a message ID."""
        import subprocess
        try:
            # Search in running daemon's output (captured by shell)
            # Also check common log locations
            result = subprocess.run(
                ["grep", "-h", f"msg_id={msg_id}", "/tmp/claude-telegram-daemon.log"],
                capture_output=True, text=True
            )
            lines = result.stdout.strip().split("\n") if result.stdout.strip() else []

            # Also try to get from recent shell output if available
            # This won't work perfectly but gives us something
            if not lines:
                return None

            return lines[:20]  # Limit to 20 lines
        except Exception as e:
            log(f"  Error searching logs: {e}")
            return None

    def _handle_debug(self, msg_id: int, reply_to: int, user_note: str = ""):
        """Handle /debug command - inject debug info into Claude conversation."""
        log(f"  /debug for msg_id={reply_to}")
        reply_to_str = str(reply_to)

        if reply_to_str not in self.state:
            # Search logs for this message ID
            log_info = self._search_logs_for_msg(reply_to)
            if log_info:
                # Inject log info into active pane
                pane = self._get_active_pane()
                if pane and pane_exists(pane):
                    lines = [f"[DEBUG] msg_id={reply_to} (not in state, from logs)", ""]
                    lines.extend(log_info)
                    if send_to_pane(pane, "\n".join(lines)):
                        react_to_message(self.bot_token, self.chat_id, msg_id)
                        return
            send_reply(self.bot_token, self.chat_id, msg_id,
                       f"msg_id={reply_to} not in state, no log entries found")
            return

        entry = self.state.get(reply_to_str)
        pane = entry.get("pane")

        if not pane or not pane_exists(pane):
            send_reply(self.bot_token, self.chat_id, msg_id,
                       f"Pane {pane} not available")
            return

        # Build debug info
        lines = [f"[DEBUG] Telegram msg_id={reply_to}"]
        if user_note:
            lines.append(f"User note: {user_note}")
        msg_type = entry.get("type", "unknown")
        lines.append(f"Type: {msg_type}")
        lines.append(f"Pane: {pane}")
        lines.append(f"CWD: {entry.get('cwd', 'N/A')}")

        notified_at = entry.get("notified_at")
        if notified_at:
            ts = datetime.datetime.fromtimestamp(notified_at).strftime("%H:%M:%S")
            elapsed = time.time() - notified_at
            lines.append(f"Notified: {ts} ({elapsed:.1f}s ago)")

        if msg_type == "permission_prompt":
            lines.append(f"Tool: {entry.get('tool_name', 'N/A')}")
            tool_id = entry.get("tool_use_id", "")
            lines.append(f"Tool ID: {tool_id}")
            lines.append(f"Handled: {entry.get('handled', False)}")
            transcript_path = entry.get("transcript_path")
            if transcript_path and tool_id:
                has_result = tool_already_handled(transcript_path, tool_id)
                lines.append(f"Has result in transcript: {has_result}")
        elif msg_type == "idle":
            lines.append(f"Claude msg ID: {entry.get('claude_msg_id', '')}")

        lines.append(f"Full state: {json.dumps(entry)}")

        if send_to_pane(pane, "\n".join(lines)):
            react_to_message(self.bot_token, self.chat_id, msg_id)
            log(f"  Injected debug info into pane {pane}")
        else:
            send_reply(self.bot_token, self.chat_id, msg_id, "Failed to send to pane")

    def _handle_todo(self, msg_id: int, text: str):
        """Handle /todo command - inject todo item into Claude conversation."""
        # Extract todo text after /todo
        todo_text = text[5:].strip()  # Remove "/todo"
        if not todo_text:
            send_reply(self.bot_token, self.chat_id, msg_id,
                       "Usage: /todo <item>")
            return

        pane = self._get_active_pane()
        if not pane or not pane_exists(pane):
            send_reply(self.bot_token, self.chat_id, msg_id,
                       "No active pane found")
            return

        # Inject todo into Claude conversation
        todo_msg = f"[TODO] {todo_text}"
        if send_to_pane(pane, todo_msg):
            react_to_message(self.bot_token, self.chat_id, msg_id)
            log(f"  Injected todo into pane {pane}: {todo_text[:50]}...")
        else:
            send_reply(self.bot_token, self.chat_id, msg_id, "Failed to send to pane")

    def _handle_setup(self, msg: dict, msg_id: int):
        """Handle /setup command - initialize group as Claude Army control center."""
        chat = msg.get("chat", {})
        chat_id = chat.get("id")
        chat_type = chat.get("type")

        log(f"  /setup in chat {chat_id} (type: {chat_type})")

        # Must be a group chat
        if chat_type not in ("group", "supergroup"):
            send_reply(self.bot_token, self.chat_id, msg_id,
                       "This command only works in group chats.")
            return

        config = get_config()

        # Check if already configured elsewhere
        if config.is_configured():
            existing_group = config.group_id
            if existing_group != chat_id:
                send_reply(self.bot_token, self.chat_id, msg_id,
                           f"Already configured for another group (ID: {existing_group}). "
                           "Run /reset in that group first.")
                return
            send_reply(self.bot_token, self.chat_id, msg_id,
                       "Already set up in this group.")
            return

        # Check if forum is enabled
        if not is_forum_enabled(self.bot_token, str(chat_id)):
            send_reply(self.bot_token, self.chat_id, msg_id,
                       "This group needs to be a Forum (supergroup with topics enabled).\n\n"
                       "To enable:\n"
                       "1. Open group settings\n"
                       "2. Go to 'Topics'\n"
                       "3. Enable topics\n\n"
                       "Then run /setup again.")
            return

        # Store configuration
        config.group_id = chat_id
        # Note: We use the General topic (thread_id=1) for now
        # In forums, message_thread_id=1 is typically the General topic
        config.general_topic_id = 1

        send_reply(self.bot_token, self.chat_id, msg_id,
                   "Claude Army initialized!\n\n"
                   "This group is now the control center. "
                   "Send messages here to interact with the Operator Claude.\n\n"
                   "Use /help to see available commands.")
        log(f"  Setup complete for group {chat_id}")

    def _handle_reset(self, msg: dict, msg_id: int):
        """Handle /reset command - remove Claude Army configuration."""
        chat = msg.get("chat", {})
        chat_id = chat.get("id")

        log(f"  /reset in chat {chat_id}")

        config = get_config()

        if not config.is_configured():
            send_reply(self.bot_token, self.chat_id, msg_id,
                       "Claude Army is not configured.")
            return

        if config.group_id != chat_id:
            send_reply(self.bot_token, self.chat_id, msg_id,
                       "Claude Army is configured for a different group. "
                       "Run /reset in that group.")
            return

        # Clear configuration
        config.clear()
        send_reply(self.bot_token, self.chat_id, msg_id,
                   "Claude Army configuration cleared. "
                   "You can run /setup in any group to reconfigure.")
        log(f"  Reset complete for group {chat_id}")

    def _handle_help(self, msg_id: int):
        """Handle /help command - show available commands."""
        config = get_config()

        help_text = """*Claude Army Commands*

/setup - Initialize this group as control center
/reset - Remove Claude Army configuration
/help - Show this help message

*In task topics:*
/debug - Debug a notification (reply to it)
/todo <item> - Add todo item for Claude

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

        send_reply(self.bot_token, self.chat_id, msg_id, help_text)
