"""Operator Claude session management."""

import subprocess
import time

from pathlib import Path

from telegram_utils import log
from registry import get_config

# Short prefix to avoid collisions with user sessions
SESSION_PREFIX = "ca-"  # claude-army
OPERATOR_SESSION = f"{SESSION_PREFIX}op"
OPERATOR_DIR = Path(__file__).parent / "operator"


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

    # Ensure operator directory exists
    OPERATOR_DIR.mkdir(parents=True, exist_ok=True)

    # Create symlinks to specs if they don't exist
    symlinks = {
        "OPERATOR_SPEC.md": "../OPERATOR_SPEC.md",
        "AGENTS.md": "../OPERATOR_AGENTS.md",  # Operator's instructions
        "CLAUDE.md": "AGENTS.md",  # Claude reads CLAUDE.md by default
    }
    for name, target in symlinks.items():
        link = OPERATOR_DIR / name
        if not link.exists():
            link.symlink_to(target)

    # Create new detached tmux session in operator directory
    result = subprocess.run(
        ["tmux", "new-session", "-d", "-s", OPERATOR_SESSION, "-c", str(OPERATOR_DIR)],
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

    # Start Claude - try continue (auto-resumes most recent), fall back to fresh
    subprocess.run(["tmux", "send-keys", "-t", pane, "claude --continue || claude", "Enter"])

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
