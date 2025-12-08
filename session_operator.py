"""Operator Claude session management."""

import subprocess
import time

from telegram_utils import log
from registry import get_config

OPERATOR_SESSION = "claude-operator"


def session_exists(session_name: str = OPERATOR_SESSION) -> bool:
    """Check if a tmux session exists."""
    result = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        capture_output=True
    )
    return result.returncode == 0


def get_pane_id(session_name: str = OPERATOR_SESSION) -> str | None:
    """Get the pane ID for a session (e.g., 'claude-operator:0.0')."""
    result = subprocess.run(
        ["tmux", "list-panes", "-t", session_name, "-F", "#{session_name}:#{window_index}.#{pane_index}"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return None
    lines = result.stdout.strip().split("\n")
    return lines[0] if lines else None


def start_operator_session() -> str | None:
    """Start the Operator Claude session. Returns pane ID on success."""
    if session_exists():
        log("Operator session already exists")
        pane = get_pane_id()
        if pane:
            config = get_config()
            config.operator_pane = pane
        return pane

    log("Starting Operator Claude session...")

    # Create new detached tmux session
    result = subprocess.run(
        ["tmux", "new-session", "-d", "-s", OPERATOR_SESSION],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        log(f"Failed to create session: {result.stderr}")
        return None

    time.sleep(0.2)

    pane = get_pane_id()
    if not pane:
        log("Failed to get pane ID")
        return None

    # Start Claude with operator context
    # Using --resume so it picks up any existing conversation
    subprocess.run(["tmux", "send-keys", "-t", pane, "claude --resume", "Enter"])

    config = get_config()
    config.operator_pane = pane

    log(f"Operator session started: {pane}")
    return pane


def stop_operator_session() -> bool:
    """Stop the Operator Claude session."""
    if not session_exists():
        return True

    result = subprocess.run(
        ["tmux", "kill-session", "-t", OPERATOR_SESSION],
        capture_output=True
    )

    if result.returncode == 0:
        config = get_config()
        config.delete("operator_pane")
        log("Operator session stopped")
        return True
    return False


def send_to_operator(text: str) -> bool:
    """Send text to the Operator Claude pane. Lazily resurrects if needed."""
    config = get_config()

    if not config.is_configured():
        log("Operator not configured")
        return False

    # Try to get existing pane, or resurrect
    pane = config.operator_pane
    if not pane or not session_exists():
        pane = check_and_resurrect_operator()

    if not pane:
        log("Failed to get operator pane")
        return False

    try:
        subprocess.run(["tmux", "send-keys", "-t", pane, "C-u"], check=True)
        subprocess.run(["tmux", "send-keys", "-t", pane, "-l", text], check=True)
        time.sleep(0.1)
        subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], check=True)
        return True
    except subprocess.CalledProcessError as e:
        log(f"Failed to send to operator: {e}")
        return False


def check_and_resurrect_operator() -> str | None:
    """Check if operator session exists, resurrect if needed. Returns pane ID."""
    config = get_config()

    if not config.is_configured():
        return None

    if session_exists():
        pane = get_pane_id()
        if pane and pane != config.operator_pane:
            config.operator_pane = pane
        return pane

    log("Operator session missing, resurrecting...")
    return start_operator_session()


def is_operator_pane(pane: str) -> bool:
    """Check if a pane is the operator pane."""
    config = get_config()
    return pane == config.operator_pane
