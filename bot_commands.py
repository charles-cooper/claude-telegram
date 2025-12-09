"""Bot command handlers for Telegram integration."""

import datetime
import json

from telegram_utils import (
    State, send_reply, send_chat_action, log, is_forum_enabled
)
from registry import get_config, get_registry, rebuild_registry_from_markers
from session_operator import start_operator_session, send_to_operator


def parse_command_args(text: str) -> str | None:
    """Extract arguments from a command, handling @botname suffix.

    "/spawn@mybot foo bar" -> "foo bar"
    "/spawn foo bar" -> "foo bar"
    "/spawn" -> None
    """
    parts = text.split(None, 1)
    if len(parts) < 2:
        return None
    return parts[1].strip()


def build_spawn_prompt(request: str, task_name: str = None, task_data: dict = None, reply_ctx: str = None) -> str:
    """Build the spawn request prompt for operator."""
    lines = ["=" * 40]
    lines.append("SPAWN REQUEST")
    lines.append("=" * 40)
    lines.append("")

    if task_name and task_data:
        lines.append(f"From task: {task_name}")
        lines.append(f"Type: {task_data.get('type', 'session')}")
        lines.append(f"Path: {task_data.get('path', '?')}")
        lines.append("")

    if reply_ctx:
        lines.append("Context:")
        lines.append(reply_ctx)
        lines.append("")

    lines.append(f"Request: {request}")
    lines.append("")
    lines.append("-" * 40)
    lines.append("Please spawn a new task to handle this request.")
    lines.append("Use spawn_task() or spawn_worktree() as appropriate.")
    lines.append("-" * 40)
    return "\n".join(lines)


def build_cleanup_prompt(task_name: str, task_data: dict) -> str:
    """Build the cleanup request prompt for operator."""
    lines = ["=" * 40]
    lines.append("CLEANUP REQUEST")
    lines.append("=" * 40)
    lines.append("")
    lines.append(f"Task: {task_name}")
    lines.append(f"Type: {task_data.get('type', 'session')}")
    lines.append(f"Path: {task_data.get('path', '?')}")
    lines.append(f"Topic ID: {task_data.get('topic_id', '?')}")
    lines.append(f"Status: {task_data.get('status', '?')}")
    lines.append("")
    lines.append("-" * 40)
    lines.append("Run cleanup_task to clean up:")
    lines.append("")
    lines.append("from session_worker import cleanup_task")
    lines.append(f"cleanup_task('{task_name}')  # deletes topic")
    lines.append(f"cleanup_task('{task_name}', archive_only=True)  # keeps topic (archived)")
    lines.append("-" * 40)
    return "\n".join(lines)


class CommandHandler:
    """Handles bot commands like /debug, /todo, /setup."""

    def __init__(self, bot_token: str, chat_id: str, state: State):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.state = state

    def _reply(self, chat_id: str, msg_id: int, text: str, parse_mode: str = "Markdown"):
        """Send a reply to a message."""
        send_reply(self.bot_token, chat_id, msg_id, text, parse_mode)

    def _typing(self, chat_id: str, topic_id: int = None):
        """Show typing indicator."""
        send_chat_action(self.bot_token, chat_id, "typing", topic_id)

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
        chat_id = str(msg.get("chat", {}).get("id"))
        topic_id = msg.get("message_thread_id")
        # Strip @botname suffix from commands (e.g., /setup@mybot -> /setup)
        text_lower = text.lower().split("@")[0] if text.startswith("/") else text.lower()

        # /todo - route to operator
        if text_lower.startswith("/todo"):
            self._handle_todo(msg, chat_id, msg_id, text, topic_id)
            return True

        # /debug - route to operator (requires reply)
        if text_lower.startswith("/debug") or (msg.get("reply_to_message") and text_lower in ("debug", "?")):
            self._handle_debug(msg, chat_id, msg_id, text)
            return True

        # /setup - initialize group
        if text_lower.startswith("/setup"):
            self._handle_setup(msg, chat_id, msg_id)
            return True

        # /help - show commands
        if text_lower.startswith("/help"):
            self._handle_help(chat_id, msg_id)
            return True

        # /status - show task status
        if text_lower.startswith("/status"):
            self._handle_status(chat_id, msg_id)
            return True

        # /spawn - create a new task (routes to operator)
        if text_lower.startswith("/spawn"):
            self._handle_spawn(msg, chat_id, msg_id, text, topic_id)
            return True

        # /cleanup - clean up a task (routes to operator)
        if text_lower.startswith("/cleanup"):
            self._handle_cleanup(msg, chat_id, msg_id, text, topic_id)
            return True

        # /tmux - show tmux attach command
        if text_lower.startswith("/tmux"):
            self._handle_tmux(chat_id, msg_id, topic_id)
            return True

        # /show - dump tmux pane output
        if text_lower.startswith("/show"):
            self._handle_show(chat_id, msg_id, topic_id)
            return True

        # /rebuild-registry - maintenance command to rebuild from markers
        if text_lower.startswith("/rebuild-registry"):
            self._handle_rebuild_registry(chat_id, msg_id)
            return True

        return False

    def _handle_todo(self, msg: dict, chat_id: str, msg_id: int, text: str, topic_id: int | None):
        """Handle /todo - send rich prompt to operator with context."""
        todo_text = text[5:].strip()  # Remove "/todo"
        if not todo_text:
            self._reply(chat_id, msg_id, "Usage: /todo <item>")
            return

        # Get task context from registry if from a task topic
        registry = get_registry()
        task_name = None
        task_data = None
        if topic_id:
            result = registry.find_task_by_topic(topic_id)
            if result:
                task_name, task_data = result

        # Build rich prompt for operator
        lines = ["=" * 40]
        lines.append("NEW TODO ITEM")
        lines.append("=" * 40)
        lines.append("")

        if task_name and task_data:
            lines.append(f"From task: {task_name}")
            lines.append(f"Registry: {json.dumps(task_data, indent=2)}")
            lines.append("")

        # Include reply context if present
        reply_ctx = self._format_reply_context(msg)
        if reply_ctx:
            lines.append("Context:")
            lines.append(reply_ctx)
            lines.append("")

        lines.append(f"Request: {todo_text}")
        lines.append("")
        lines.append("-" * 40)
        lines.append("Please investigate this in the relevant repo/codebase.")
        lines.append("Gather context, understand the issue, and either:")
        lines.append("  1. Handle it yourself if simple")
        lines.append("  2. Spawn/delegate to a worker with clear instructions")
        lines.append("  3. Ask clarifying questions if needed")
        lines.append("-" * 40)

        if send_to_operator("\n".join(lines)):
            self._typing(chat_id, topic_id)
            log(f"  /todo sent to operator: {todo_text[:50]}...")
        else:
            self._reply(chat_id, msg_id, "Operator not available")

    def _handle_debug(self, msg: dict, chat_id: str, msg_id: int, text: str):
        """Handle /debug - dump debug info for a message."""
        if not msg.get("reply_to_message"):
            self._reply(chat_id, msg_id, "Reply to a message to debug it")
            return

        reply_to = msg["reply_to_message"]
        reply_to_id = reply_to.get("message_id")
        reply_str = str(reply_to_id)

        # Build debug output
        lines = [f"*Debug: msg_id={reply_to_id}*"]

        # Message metadata
        reply_from = reply_to.get("from", {})
        lines.append(f"From: {reply_from.get('first_name', '?')} (id={reply_from.get('id', '?')})")
        lines.append(f"Date: {reply_to.get('date', '?')}")

        # Text preview
        reply_text = reply_to.get("text", "")
        if reply_text:
            preview = reply_text[:100] + "..." if len(reply_text) > 100 else reply_text
            lines.append(f"Text: {preview}")

        # State info
        if reply_str in self.state:
            entry = self.state.get(reply_str)
            lines.append("")
            lines.append("*State:*")
            lines.append(f"```\n{json.dumps(entry, indent=2)}\n```")
        else:
            lines.append("\n_No state tracked for this message_")

        self._reply(chat_id, msg_id, "\n".join(lines))
        log(f"  /debug for msg_id={reply_to_id}")

    def _handle_setup(self, msg: dict, chat_id: str, msg_id: int):
        """Handle /setup - initialize group as Claude Army control center."""
        chat = msg.get("chat", {})
        chat_id_int = chat.get("id")
        chat_type = chat.get("type")

        log(f"  /setup in chat {chat_id_int} (type: {chat_type})")

        if chat_type not in ("group", "supergroup"):
            self._reply(chat_id, msg_id,
                "To set up Claude Army:\n\n"
                "1. Create a new Telegram group\n"
                "2. Add this bot to the group as admin\n"
                "3. Open group settings -> Topics -> Enable\n"
                "4. Run /setup in the group")
            return

        config = get_config()

        if config.is_configured():
            if config.group_id != chat_id_int:
                self._reply(chat_id, msg_id,
                    f"Already configured for another group (ID: {config.group_id}). "
                    "Run /reset in that group first.")
                return
            self._reply(chat_id, msg_id, "Already set up in this group.")
            return

        if not is_forum_enabled(self.bot_token, str(chat_id_int)):
            self._reply(chat_id, msg_id,
                "This group needs to be a Forum (supergroup with topics enabled).\n\n"
                "To enable:\n"
                "1. Open group settings\n"
                "2. Go to 'Topics'\n"
                "3. Enable topics\n\n"
                "Then run /setup again.")
            return

        # Store configuration
        config.group_id = chat_id_int
        config.general_topic_id = 1  # General topic in forums

        # Start operator session
        pane = start_operator_session()
        if pane:
            self._reply(chat_id, msg_id,
                "Claude Army initialized!\n\n"
                "Operator Claude is running. Send messages here to interact.\n\n"
                "Use /help to see available commands.")
        else:
            self._reply(chat_id, msg_id,
                "Claude Army configured, but failed to start Operator session.\n"
                "Check tmux availability.")

        log(f"  Setup complete for group {chat_id_int}")

    def _handle_status(self, chat_id: str, msg_id: int):
        """Handle /status - show all tasks and their status."""
        config = get_config()
        registry = get_registry()

        if not config.is_configured():
            self._reply(chat_id, msg_id, "Claude Army not configured. Run /setup first.")
            return

        tasks = registry.get_all_tasks()
        if not tasks:
            self._reply(chat_id, msg_id, "No active tasks.")
            return

        lines = ["*Task Status*\n"]
        for task_name, task_data in tasks:
            status = task_data.get("status", "unknown")
            task_type = task_data.get("type", "session")
            topic_id = task_data.get("topic_id", "?")
            emoji = "▶️" if status == "active" else "⏸️" if status == "paused" else "❓"
            type_indicator = "🌳" if task_type == "worktree" else "📁"
            lines.append(f"{emoji}{type_indicator} `{task_name}` ({status})")

        self._reply(chat_id, msg_id, "\n".join(lines))

    def _handle_rebuild_registry(self, chat_id: str, msg_id: int):
        """Handle /rebuild-registry - rebuild registry from marker files.

        This is a maintenance command for edge cases where the registry gets
        out of sync with actual marker files on disk.
        """
        config = get_config()

        if not config.is_configured():
            self._reply(chat_id, msg_id, "Claude Army not configured. Run /setup first.")
            return

        self._reply(chat_id, msg_id, "Scanning for marker files...")
        recovered = rebuild_registry_from_markers()

        if recovered > 0:
            self._reply(chat_id, msg_id, f"Recovered {recovered} task(s). Run /status to see them.")
        else:
            self._reply(chat_id, msg_id, "No new tasks found.")

    def _handle_spawn(self, msg: dict, chat_id: str, msg_id: int, text: str, topic_id: int | None):
        """Handle /spawn - route spawn request to operator."""
        request = parse_command_args(text)
        if not request:
            self._reply(chat_id, msg_id, "Usage: /spawn <description of task to create>")
            return

        # Get task context from registry if from a task topic
        registry = get_registry()
        task_name = None
        task_data = None
        if topic_id:
            result = registry.find_task_by_topic(topic_id)
            if result:
                task_name, task_data = result

        # Include reply context if present
        reply_ctx = self._format_reply_context(msg)

        prompt = build_spawn_prompt(request, task_name, task_data, reply_ctx)
        if send_to_operator(prompt):
            self._typing(chat_id, topic_id)
            log(f"  /spawn sent to operator: {request[:50]}...")

            # Reply with link to operator topic
            config = get_config()
            group_id = config.group_id
            general_topic = config.general_topic_id or 1
            link_chat_id = str(group_id).replace("-100", "")
            link = f"https://t.me/c/{link_chat_id}/{general_topic}"
            self._reply(chat_id, msg_id, f"Sent to [Operator]({link})")
        else:
            self._reply(chat_id, msg_id, "Operator not available")

    def _handle_cleanup(self, msg: dict, chat_id: str, msg_id: int, text: str, topic_id: int | None):
        """Handle /cleanup - route cleanup request to operator."""
        registry = get_registry()
        task_name = parse_command_args(text)

        # If no task name provided, try to infer from topic
        if not task_name and topic_id:
            result = registry.find_task_by_topic(topic_id)
            if result:
                task_name, _ = result

        if not task_name:
            tasks = registry.get_all_tasks()
            if tasks:
                task_list = ", ".join(name for name, _ in tasks)
                self._reply(chat_id, msg_id, f"Usage: /cleanup <task_name>\n\nAvailable tasks: {task_list}")
            else:
                self._reply(chat_id, msg_id, "Usage: /cleanup <task_name>\n\nNo active tasks.")
            return

        task_data = registry.get_task(task_name)
        if not task_data:
            self._reply(chat_id, msg_id, f"Task '{task_name}' not found. Run /status to see tasks.")
            return

        prompt = build_cleanup_prompt(task_name, task_data)
        send_to_operator(prompt)
        log(f"  /cleanup sent to operator: {task_name}")

        # Reply with link to operator topic
        config = get_config()
        group_id = config.group_id
        general_topic = config.general_topic_id or 1
        # Telegram uses chat_id without -100 prefix for links
        link_chat_id = str(group_id).replace("-100", "")
        link = f"https://t.me/c/{link_chat_id}/{general_topic}"
        self._reply(chat_id, msg_id, f"Sent to [Operator]({link})")

    def _get_pane_for_topic(self, topic_id: int | None) -> tuple[str, str] | None:
        """Get (task_name, pane) for a topic. Returns operator for General topic."""
        config = get_config()
        is_general = topic_id is None or topic_id == config.general_topic_id
        if is_general:
            pane = config.operator_pane
            return ("operator", pane) if pane else None

        registry = get_registry()
        result = registry.find_task_by_topic(topic_id)
        if result:
            task_name, task_data = result
            return (task_name, task_data.get("pane"))
        return None

    def _handle_tmux(self, chat_id: str, msg_id: int, topic_id: int | None):
        """Handle /tmux - show tmux attach command for task."""
        result = self._get_pane_for_topic(topic_id)
        if not result:
            self._reply(chat_id, msg_id, "Send from a task topic to get its tmux command.")
            return

        task_name, pane = result
        session = pane.split(":")[0] if pane and ":" in pane else pane

        if session:
            self._reply(chat_id, msg_id, f"`tmux attach -t {session}`")
        else:
            self._reply(chat_id, msg_id, f"No tmux session found for '{task_name}'.")

    def _handle_show(self, chat_id: str, msg_id: int, topic_id: int | None):
        """Handle /show - dump tmux pane output."""
        import subprocess

        result = self._get_pane_for_topic(topic_id)
        if not result:
            self._reply(chat_id, msg_id, "Send from a task topic to show its output.")
            return

        task_name, pane = result
        if not pane:
            self._reply(chat_id, msg_id, f"No tmux pane found for '{task_name}'.")
            return

        try:
            output = subprocess.run(
                ["tmux", "capture-pane", "-t", pane, "-p", "-S", "-50"],
                capture_output=True, text=True, timeout=5
            )
            if output.returncode != 0:
                self._reply(chat_id, msg_id, f"Failed to capture pane: {output.stderr}")
                return

            text = output.stdout.strip()
            if not text:
                self._reply(chat_id, msg_id, "_Pane is empty_")
                return

            # Truncate if too long for Telegram
            if len(text) > 3500:
                text = text[-3500:]
                text = "...\n" + text

            self._reply(chat_id, msg_id, f"```\n{text}\n```")
        except subprocess.TimeoutExpired:
            self._reply(chat_id, msg_id, "Timeout capturing pane.")
        except Exception as e:
            self._reply(chat_id, msg_id, f"Error: {e}")

    def _handle_help(self, chat_id: str, msg_id: int):
        """Handle /help - show available commands."""
        config = get_config()

        help_text = """*Claude Army Commands*

/setup - Initialize this group as control center
/status - Show all tasks and status
/spawn <desc> - Create a new task
/cleanup - Clean up current task
/tmux - Show tmux attach command
/show - Dump tmux pane output
/help - Show this help message
/todo <item> - Add todo to Operator queue
/debug - Show debug info for a message (reply to it)
/rebuild-registry - Rebuild registry from markers (maintenance)

*Operator Commands* (natural language):
- "Create task X in repo Y"
- "What's the status?"
- "Pause/resume task X"
"""
        if config.is_configured():
            help_text += f"\n_Status: Configured (group {config.group_id})_"
        else:
            help_text += "\n_Status: Not configured_"

        self._reply(chat_id, msg_id, help_text)
