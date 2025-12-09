"""Worker Claude session management.

Supports two task types:
- Worktree: isolated git worktree, cleanup deletes directory
- Session: existing directory, cleanup preserves directory
"""

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from telegram_utils import (
    log, edit_forum_topic, create_forum_topic, close_forum_topic,
    shell_quote, TopicCreationError
)
from registry import (
    get_config, get_registry, write_marker_file, read_marker_file,
    remove_marker_file
)


# Short prefix to avoid collisions with user sessions
SESSION_PREFIX = "ca-"  # claude-army

SETUP_HOOK_NAME = ".claude-army-setup.sh"


def _get_bot_token() -> str:
    """Get bot token from telegram config."""
    tg_config = json.loads((Path.home() / "telegram.json").read_text())
    return tg_config["bot_token"]


def _get_session_name(task_name: str) -> str:
    """Get tmux session name for a task."""
    return f"{SESSION_PREFIX}{task_name}"


def _session_exists(session_name: str) -> bool:
    """Check if a tmux session exists."""
    result = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        capture_output=True
    )
    return result.returncode == 0


def _get_pane_id(session_name: str) -> str | None:
    """Get the pane ID for a session."""
    result = subprocess.run(
        ["tmux", "list-panes", "-t", session_name, "-F", "#{session_name}:#{window_index}.#{pane_index}"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return None
    lines = result.stdout.strip().split("\n")
    return lines[0] if lines else None


def _create_tmux_session(session_name: str, directory: str) -> str | None:
    """Create a tmux session in directory. Returns pane ID."""
    result = subprocess.run(
        ["tmux", "new-session", "-d", "-s", session_name, "-c", directory],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        log(f"Failed to create session: {result.stderr}")
        return None

    time.sleep(0.2)
    return _get_pane_id(session_name)


def _kill_tmux_session(session_name: str) -> bool:
    """Kill a tmux session."""
    if not _session_exists(session_name):
        return True
    result = subprocess.run(
        ["tmux", "kill-session", "-t", session_name],
        capture_output=True
    )
    return result.returncode == 0


def _start_claude(pane: str, description: str, resume: bool = False):
    """Start Claude in a pane."""
    if resume:
        cmd = f"claude --continue || claude {shell_quote(description)}"
    else:
        cmd = f"claude {shell_quote(description)}"
    subprocess.run(["tmux", "send-keys", "-t", pane, cmd, "Enter"])


def update_topic_status(topic_id: int, task_name: str, status: str):
    """Update topic name to reflect task status."""
    # No status prefixes for now - topic name stays as task_name
    # TODO: Could add status suffix like "(paused)" if desired
    pass


# ============ Worktree Operations ============

def get_worktree_path(repo_path: str, task_name: str) -> Path:
    """Get the worktree path for a task."""
    return Path(repo_path) / "trees" / task_name


def run_setup_hook(repo_path: str, task_name: str, worktree_path: Path) -> bool:
    """Run post-worktree setup hook if it exists."""
    hook_path = Path(repo_path) / SETUP_HOOK_NAME
    if not hook_path.exists():
        return True

    log(f"Running setup hook: {hook_path}")
    env = {
        **os.environ,
        "TASK_NAME": task_name,
        "REPO_PATH": repo_path,
        "WORKTREE_PATH": str(worktree_path)
    }

    result = subprocess.run(
        ["bash", str(hook_path)],
        cwd=str(worktree_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=60
    )

    if result.returncode != 0:
        log(f"Setup hook failed: {result.stderr}")
        return False

    log("Setup hook completed")
    return True


def create_worktree(repo_path: str, task_name: str, branch: str = None) -> Path | None:
    """Create a git worktree for a task. Returns worktree path on success."""
    worktree_path = get_worktree_path(repo_path, task_name)

    if worktree_path.exists():
        log(f"Worktree already exists: {worktree_path}")
        return worktree_path

    worktree_path.parent.mkdir(parents=True, exist_ok=True)

    branch_arg = branch if branch else "HEAD"
    cmd = ["git", "-C", repo_path, "worktree", "add", "-b", task_name, str(worktree_path), branch_arg]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        # Try without creating new branch (if branch exists)
        cmd = ["git", "-C", repo_path, "worktree", "add", str(worktree_path), task_name]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            log(f"Failed to create worktree: {result.stderr}")
            return None

    log(f"Created worktree: {worktree_path}")
    run_setup_hook(repo_path, task_name, worktree_path)
    return worktree_path


def delete_worktree(repo_path: str, worktree_path: str) -> bool:
    """Delete a git worktree."""
    if not Path(worktree_path).exists():
        return True

    result = subprocess.run(
        ["git", "-C", repo_path, "worktree", "remove", "--force", worktree_path],
        capture_output=True, text=True
    )

    if result.returncode != 0:
        log(f"Failed to remove worktree: {result.stderr}")
        return False

    log(f"Deleted worktree: {worktree_path}")
    return True


# ============ Task Spawning ============

def spawn_worktree_task(repo_path: str, task_name: str, description: str) -> dict | None:
    """Spawn a worktree task: create worktree, topic, marker, session.

    Returns task_data dict on success, None on failure.
    """
    config = get_config()
    if not config.is_configured():
        log("Not configured")
        return None

    registry = get_registry()

    # Check for name collision
    if registry.get_task(task_name):
        log(f"Task already exists: {task_name}")
        return None

    # Create worktree
    worktree_path = create_worktree(repo_path, task_name)
    if not worktree_path:
        return None

    # Create topic
    bot_token = _get_bot_token()
    try:
        topic_result = create_forum_topic(bot_token, str(config.group_id), task_name)
    except TopicCreationError:
        delete_worktree(repo_path, str(worktree_path))
        raise

    topic_id = topic_result.get("message_thread_id")

    # Create tmux session
    session_name = _get_session_name(task_name)
    pane = _create_tmux_session(session_name, str(worktree_path))
    if not pane:
        close_forum_topic(bot_token, str(config.group_id), topic_id)
        delete_worktree(repo_path, str(worktree_path))
        return None

    # Write marker file
    marker_data = {
        "name": task_name,
        "type": "worktree",
        "repo": repo_path,
        "description": description,
        "topic_id": topic_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    write_marker_file(str(worktree_path), marker_data)

    # Update registry
    task_data = {
        "type": "worktree",
        "path": str(worktree_path),
        "repo": repo_path,
        "topic_id": topic_id,
        "pane": pane,
        "status": "active",
    }
    registry.add_task(task_name, task_data)

    # Start Claude
    _start_claude(pane, description)

    log(f"Spawned worktree task: {task_name} at {worktree_path}")
    return task_data


def spawn_session(directory: str, task_name: str, description: str) -> dict | None:
    """Spawn a session task in existing directory: create topic, marker, session.

    Returns task_data dict on success, None on failure.
    """
    config = get_config()
    if not config.is_configured():
        log("Not configured")
        return None

    if not Path(directory).exists():
        log(f"Directory doesn't exist: {directory}")
        return None

    registry = get_registry()

    # Check for name collision
    if registry.get_task(task_name):
        log(f"Task already exists: {task_name}")
        return None

    # Create topic
    bot_token = _get_bot_token()
    topic_result = create_forum_topic(bot_token, str(config.group_id), task_name)
    topic_id = topic_result.get("message_thread_id")

    # Create tmux session
    session_name = _get_session_name(task_name)
    pane = _create_tmux_session(session_name, directory)
    if not pane:
        close_forum_topic(bot_token, str(config.group_id), topic_id)
        return None

    # Write marker file
    marker_data = {
        "name": task_name,
        "type": "session",
        "description": description,
        "topic_id": topic_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    write_marker_file(directory, marker_data)

    # Update registry
    task_data = {
        "type": "session",
        "path": directory,
        "topic_id": topic_id,
        "pane": pane,
        "status": "active",
    }
    registry.add_task(task_name, task_data)

    # Start Claude
    _start_claude(pane, description)

    log(f"Spawned session: {task_name} at {directory}")
    return task_data


def register_existing_session(directory: str, task_name: str) -> dict | None:
    """Register an existing Claude session (auto-registration by daemon).

    Creates topic and marker for a discovered session.
    Returns task_data dict on success, None if not configured or name collision.
    Raises TopicCreationError if topic creation fails.
    """
    config = get_config()
    if not config.is_configured():
        return None

    registry = get_registry()

    # Check for name collision
    if registry.get_task(task_name):
        return None

    # Create topic (raises TopicCreationError on failure)
    bot_token = _get_bot_token()
    topic_result = create_forum_topic(bot_token, str(config.group_id), task_name)
    topic_id = topic_result.get("message_thread_id")

    # Write marker file (session already exists, we don't create tmux session)
    marker_data = {
        "name": task_name,
        "type": "session",
        "topic_id": topic_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    write_marker_file(directory, marker_data)

    # Update registry (pane will be updated when we detect it)
    task_data = {
        "type": "session",
        "path": directory,
        "topic_id": topic_id,
        "status": "active",
    }
    registry.add_task(task_name, task_data)

    log(f"Registered existing session: {task_name} at {directory}")
    return task_data


# ============ Task Operations ============

def stop_task_session(task_name: str) -> bool:
    """Stop the tmux session for a task."""
    session_name = _get_session_name(task_name)
    if _kill_tmux_session(session_name):
        log(f"Stopped session: {session_name}")
        return True
    return False


def pause_task(task_name: str) -> bool:
    """Pause a task (stop session, mark as paused)."""
    registry = get_registry()
    task_data = registry.get_task(task_name)
    if not task_data:
        return False

    topic_id = task_data.get("topic_id")
    path = task_data.get("path")

    # Stop session
    stop_task_session(task_name)

    # Update marker
    marker = read_marker_file(path)
    if marker:
        marker["status"] = "paused"
        write_marker_file(path, marker)

    # Update registry
    task_data["status"] = "paused"
    task_data.pop("pane", None)
    registry.add_task(task_name, task_data)

    # Update topic name
    if topic_id:
        update_topic_status(topic_id, task_name, "paused")

    log(f"Paused task: {task_name}")
    return True


def resume_task(task_name: str) -> str | None:
    """Resume a paused task. Returns pane ID on success."""
    registry = get_registry()
    task_data = registry.get_task(task_name)
    if not task_data:
        return None

    path = task_data.get("path")
    topic_id = task_data.get("topic_id")

    # Update marker
    marker = read_marker_file(path)
    if marker:
        marker["status"] = "active"
        write_marker_file(path, marker)

    # Create session
    session_name = _get_session_name(task_name)
    pane = _create_tmux_session(session_name, path)
    if not pane:
        return None

    # Start Claude with resume
    description = marker.get("description", task_name) if marker else task_name
    _start_claude(pane, description, resume=True)

    # Update registry
    task_data["status"] = "active"
    task_data["pane"] = pane
    registry.add_task(task_name, task_data)

    # Update topic name
    if topic_id:
        update_topic_status(topic_id, task_name, "active")

    log(f"Resumed task: {task_name}")
    return pane


def cleanup_task(task_name: str) -> bool:
    """Clean up a task. Behavior differs by type:
    - worktree: delete directory + close topic
    - session: remove marker + close topic (preserve directory)
    """
    registry = get_registry()
    task_data = registry.get_task(task_name)
    if not task_data:
        return False

    task_type = task_data.get("type", "session")
    path = task_data.get("path")
    topic_id = task_data.get("topic_id")
    repo = task_data.get("repo")

    # Stop session
    stop_task_session(task_name)

    # Update topic
    if topic_id:
        update_topic_status(topic_id, task_name, "done")
        try:
            bot_token = _get_bot_token()
            config = get_config()
            close_forum_topic(bot_token, str(config.group_id), topic_id)
        except Exception as e:
            log(f"Failed to close topic: {e}")

    # Type-specific cleanup
    if task_type == "worktree" and repo and path:
        delete_worktree(repo, path)
    elif task_type == "session" and path:
        remove_marker_file(path)

    # Remove from registry
    registry.remove_task(task_name)

    log(f"Cleaned up task: {task_name}")
    return True


# ============ Worker Communication ============

def get_worker_pane_for_topic(topic_id: int) -> str | None:
    """Get the worker pane for a topic ID."""
    registry = get_registry()
    result = registry.find_task_by_topic(topic_id)
    if result:
        name, task_data = result
        return task_data.get("pane")
    return None


def send_to_worker(topic_id: int, text: str) -> bool:
    """Send text to the worker handling a topic. Resurrects if needed."""
    registry = get_registry()
    result = registry.find_task_by_topic(topic_id)
    if not result:
        log(f"No task for topic {topic_id}")
        return False

    task_name, task_data = result
    session_name = _get_session_name(task_name)
    pane = task_data.get("pane")

    # Resurrect if needed
    if not pane or not _session_exists(session_name):
        if task_data.get("status") == "paused":
            log(f"Task {task_name} is paused, not resurrecting")
            return False
        pane = resume_task(task_name)

    if not pane:
        log(f"Failed to get pane for topic {topic_id}")
        return False

    try:
        subprocess.run(["tmux", "send-keys", "-t", pane, "C-u"], check=True)
        subprocess.run(["tmux", "send-keys", "-t", pane, "-l", text], check=True)
        time.sleep(0.1)
        subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], check=True)
        return True
    except subprocess.CalledProcessError as e:
        log(f"Failed to send to worker: {e}")
        return False


def is_worker_pane(pane: str) -> tuple[bool, int | None]:
    """Check if pane is a worker pane. Returns (is_worker, topic_id)."""
    registry = get_registry()
    result = registry.find_task_by_pane(pane)
    if result:
        name, task_data = result
        return True, task_data.get("topic_id")
    return False, None


def check_and_resurrect_task(task_name: str) -> str | None:
    """Check if task session exists, resurrect if needed. Returns pane ID."""
    registry = get_registry()
    task_data = registry.get_task(task_name)
    if not task_data:
        return None

    if task_data.get("status") == "paused":
        return None

    session_name = _get_session_name(task_name)
    if _session_exists(session_name):
        pane = _get_pane_id(session_name)
        if pane and pane != task_data.get("pane"):
            task_data["pane"] = pane
            registry.add_task(task_name, task_data)
        return pane

    log(f"Session missing, resurrecting: {task_name}")
    return resume_task(task_name)
