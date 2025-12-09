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
    log, edit_forum_topic, create_forum_topic, close_forum_topic, delete_forum_topic,
    shell_quote, TopicCreationError, send_to_tmux_pane, send_to_topic,
    escape_markdown_v2, pane_exists
)
from registry import (
    get_config, get_registry, write_marker_file, read_marker_file,
    remove_marker_file, write_marker_file_pending
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


def _find_pane_by_directory(directory: str) -> str | None:
    """Find an existing tmux pane with this directory as cwd."""
    result = subprocess.run(
        ["tmux", "list-panes", "-a", "-F", "#{session_name}:#{window_index}.#{pane_index} #{pane_current_path}"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return None

    directory = os.path.realpath(directory)
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split(" ", 1)
        if len(parts) == 2:
            pane, cwd = parts
            if os.path.realpath(cwd) == directory:
                return pane
    return None


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
    """Start Claude in a pane.

    If resume=False (new task), prompts Claude to summarize and wait for approval.
    If resume=True, continues existing conversation or falls back to description.
    """
    if resume:
        cmd = f"claude --continue || claude {shell_quote(description)}"
    else:
        confirm_prompt = (
            f"New task: {description}\n\n"
            "Please:\n"
            "1. Summarize what you understand the task to be\n"
            "2. Outline your planned approach\n"
            "3. Wait for user confirmation before starting work"
        )
        cmd = f"claude {shell_quote(confirm_prompt)}"
    subprocess.run(["tmux", "send-keys", "-t", pane, cmd, "Enter"])


def update_topic_status(topic_id: int, task_name: str, status: str):
    """Update topic name to reflect task status."""
    # No status prefixes for now - topic name stays as task_name
    # TODO: Could add status suffix like "(paused)" if desired
    pass


def _create_task_topic_safely(
    directory: str,
    task_name: str,
    task_type: str,
    description: str,
    welcome_message: str,
    repo: str = None
) -> int:
    """Create Telegram topic with crash-safe marker pattern.

    Steps:
    1. Write pending marker (so recovery knows topic creation is in progress)
    2. Create Telegram topic
    3. Send welcome message to topic
    4. Complete marker with full metadata

    Returns topic_id. Raises TopicCreationError on failure.
    Caller is responsible for cleanup if this fails after step 1.
    """
    config = get_config()
    bot_token = _get_bot_token()

    # Step 1: Write pending marker
    write_marker_file_pending(directory, task_name)

    # Step 2: Create topic (may raise TopicCreationError)
    topic_result = create_forum_topic(bot_token, str(config.group_id), task_name)
    topic_id = topic_result.get("message_thread_id")

    # Step 3: Send welcome message
    send_to_topic(bot_token, str(config.group_id), topic_id, welcome_message)

    # Step 4: Complete marker with full metadata
    marker_data = {
        "name": task_name,
        "type": task_type,
        "description": description,
        "topic_id": topic_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if repo:
        marker_data["repo"] = repo
    write_marker_file(directory, marker_data)

    return topic_id


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

    # Create topic with crash-safe pattern
    welcome = f"🚀 *Task created*\n\n_{escape_markdown_v2(description)}_"
    try:
        topic_id = _create_task_topic_safely(
            directory=str(worktree_path),
            task_name=task_name,
            task_type="worktree",
            description=description,
            welcome_message=welcome,
            repo=repo_path
        )
    except TopicCreationError:
        delete_worktree(repo_path, str(worktree_path))
        raise

    # Create tmux session
    session_name = _get_session_name(task_name)
    pane = _create_tmux_session(session_name, str(worktree_path))
    if not pane:
        bot_token = _get_bot_token()
        close_forum_topic(bot_token, str(config.group_id), topic_id)
        delete_worktree(repo_path, str(worktree_path))
        return None

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

    # Create topic with crash-safe pattern
    welcome = f"🚀 *Task created*\n\n_{escape_markdown_v2(description)}_"
    topic_id = _create_task_topic_safely(
        directory=directory,
        task_name=task_name,
        task_type="session",
        description=description,
        welcome_message=welcome
    )

    # Check for existing tmux pane in this directory
    existing_pane = _find_pane_by_directory(directory)
    if existing_pane:
        log(f"Found existing pane {existing_pane} in {directory}, reusing")
        pane = existing_pane
    else:
        # Create new tmux session
        session_name = _get_session_name(task_name)
        pane = _create_tmux_session(session_name, directory)
        if not pane:
            bot_token = _get_bot_token()
            close_forum_topic(bot_token, str(config.group_id), topic_id)
            return None

    # Update registry
    task_data = {
        "type": "session",
        "path": directory,
        "topic_id": topic_id,
        "pane": pane,
        "status": "active",
    }
    registry.add_task(task_name, task_data)

    # Only start Claude if we created a new session
    if not existing_pane:
        _start_claude(pane, description)

    log(f"Spawned session: {task_name} at {directory}")
    return task_data


def register_existing_session(directory: str, task_name: str) -> dict | None:
    """Register an existing Claude session (auto-registration by daemon).

    Uses crash-safe topic creation pattern.

    Returns task_data dict on success, None if not configured/name collision/pending.
    Raises TopicCreationError if topic creation fails.
    """
    config = get_config()
    if not config.is_configured():
        return None

    registry = get_registry()

    # Check for name collision
    if registry.get_task(task_name):
        return None

    # Check for existing marker
    existing = read_marker_file(directory)
    if existing:
        if existing.get("topic_id"):
            # Already registered, just return existing data
            task_data = {
                "type": existing.get("type", "session"),
                "path": directory,
                "topic_id": existing["topic_id"],
                "status": "active",
            }
            registry.add_task(existing.get("name", task_name), task_data)
            log(f"Recovered from existing marker: {task_name}")
            return task_data
        if existing.get("pending_topic_name"):
            # Pending recovery in progress, skip
            log(f"Pending recovery for {directory}, skipping")
            return None

    # Create topic with crash-safe pattern
    welcome = escape_markdown_v2("📡 Session discovered")
    topic_id = _create_task_topic_safely(
        directory=directory,
        task_name=task_name,
        task_type="session",
        description="",
        welcome_message=welcome
    )

    # Update registry
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

    # Create session (handle race where session already exists)
    session_name = _get_session_name(task_name)
    pane = _create_tmux_session(session_name, path)
    session_already_existed = False
    if not pane:
        # Session might already exist (race condition) - check and use if so
        if _session_exists(session_name):
            pane = _get_pane_id(session_name)
            session_already_existed = True
        if not pane:
            return None

    # Only start Claude if we created the session (avoid double-start on race)
    if not session_already_existed:
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


def cleanup_task(task_name: str, archive_only: bool = False) -> bool:
    """Clean up a task. Behavior differs by type:
    - worktree: delete directory + delete topic
    - session: remove marker + delete topic (preserve directory)

    If archive_only=True, close topic instead of deleting.
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

    # Delete or close topic
    if topic_id:
        try:
            bot_token = _get_bot_token()
            config = get_config()
            if archive_only:
                update_topic_status(topic_id, task_name, "done")
                close_forum_topic(bot_token, str(config.group_id), topic_id)
            else:
                delete_forum_topic(bot_token, str(config.group_id), topic_id)
        except Exception as e:
            log(f"Failed to {'close' if archive_only else 'delete'} topic: {e}")

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


def _find_pane_by_cwd(cwd: str) -> str | None:
    """Find a tmux pane with the given working directory."""
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{session_name}:#{window_index}.#{pane_index} #{pane_current_path}"],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            return None
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split(" ", 1)
            if len(parts) == 2 and parts[1] == cwd:
                return parts[0]
    except Exception:
        pass
    return None


def send_to_worker(topic_id: int, text: str) -> bool:
    """Send text to the worker handling a topic. Resurrects if needed."""
    registry = get_registry()
    config = get_config()
    result = registry.find_task_by_topic(topic_id)
    if not result:
        log(f"No task for topic {topic_id}")
        return False

    task_name, task_data = result
    pane = task_data.get("pane")
    path = task_data.get("path")

    # Check if stored pane exists
    if pane and pane_exists(pane):
        return send_to_tmux_pane(pane, text)

    # Stored pane doesn't exist - try to find by cwd
    if path:
        discovered_pane = _find_pane_by_cwd(path)
        if discovered_pane:
            log(f"Discovered pane {discovered_pane} for task {task_name} (was {pane})")
            task_data["pane"] = discovered_pane
            registry.add_task(task_name, task_data)
            return send_to_tmux_pane(discovered_pane, text)

    # No existing pane found - need to resurrect
    if task_data.get("status") == "paused":
        log(f"Task {task_name} is paused, not resurrecting")
        return False

    # Notify user that we're recreating the session
    bot_token = _get_bot_token()
    if bot_token and config.group_id:
        send_to_topic(bot_token, str(config.group_id), topic_id,
                      escape_markdown_v2(f"⚠️ Session not found, recreating {task_name}..."))

    pane = resume_task(task_name)
    if not pane:
        log(f"Failed to get pane for topic {topic_id}")
        if bot_token and config.group_id:
            send_to_topic(bot_token, str(config.group_id), topic_id,
                          escape_markdown_v2(f"❌ Failed to recreate {task_name}"))
        return False

    # Notify recovery complete
    if bot_token and config.group_id:
        send_to_topic(bot_token, str(config.group_id), topic_id,
                      escape_markdown_v2(f"✅ Session recreated"))

    return send_to_tmux_pane(pane, text)


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
