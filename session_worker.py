"""Worker Claude session management."""

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from telegram_utils import log, edit_forum_topic
from registry import (
    get_config, get_registry, write_marker_file, read_marker_file,
    get_marker_path, MARKER_FILE_NAME
)

# Status prefixes for topic names
STATUS_PREFIXES = {
    "active": "▶️",
    "paused": "⏸️",
    "done": "✅",
}


def update_topic_status(topic_id: int, task_name: str, status: str):
    """Update topic name to reflect task status."""
    config = get_config()
    if not config.is_configured():
        return

    prefix = STATUS_PREFIXES.get(status, "")
    new_name = f"{prefix} {task_name}".strip()

    # Get bot token from telegram config
    try:
        import json
        from pathlib import Path
        tg_config = json.loads((Path.home() / "telegram.json").read_text())
        bot_token = tg_config["bot_token"]
        edit_forum_topic(bot_token, str(config.group_id), topic_id, new_name)
        log(f"Updated topic name: {new_name}")
    except Exception as e:
        log(f"Failed to update topic name: {e}")


def get_worktree_path(repo_path: str, task_name: str) -> Path:
    """Get the worktree path for a task."""
    registry = get_registry()
    repo_data = registry.repos.get(repo_path, {})
    base = repo_data.get("worktree_base", "trees")
    return Path(repo_path) / base / task_name


SETUP_HOOK_NAME = ".claude-army-setup.sh"


def run_setup_hook(repo_path: str, task_name: str, worktree_path: Path) -> bool:
    """Run post-worktree setup hook if it exists. Returns True if ran successfully."""
    hook_path = Path(repo_path) / SETUP_HOOK_NAME
    if not hook_path.exists():
        return True  # No hook is fine

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

    log(f"Setup hook completed")
    return True


def create_worktree(repo_path: str, task_name: str, branch: str = None) -> Path | None:
    """Create a git worktree for a task. Returns worktree path on success."""
    worktree_path = get_worktree_path(repo_path, task_name)

    if worktree_path.exists():
        log(f"Worktree already exists: {worktree_path}")
        return worktree_path

    # Ensure trees directory exists
    worktree_path.parent.mkdir(parents=True, exist_ok=True)

    # Create worktree from current HEAD (or specified branch)
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

    # Run setup hook
    run_setup_hook(repo_path, task_name, worktree_path)

    return worktree_path


def delete_worktree(repo_path: str, task_name: str) -> bool:
    """Delete a git worktree."""
    worktree_path = get_worktree_path(repo_path, task_name)

    if not worktree_path.exists():
        return True

    # Remove worktree
    result = subprocess.run(
        ["git", "-C", repo_path, "worktree", "remove", "--force", str(worktree_path)],
        capture_output=True, text=True
    )

    if result.returncode != 0:
        log(f"Failed to remove worktree: {result.stderr}")
        return False

    log(f"Deleted worktree: {worktree_path}")
    return True


def get_session_name(repo_path: str, task_name: str) -> str:
    """Get tmux session name for a worker."""
    repo_name = Path(repo_path).name
    return f"claude-{repo_name}-{task_name}"


def session_exists(session_name: str) -> bool:
    """Check if a tmux session exists."""
    result = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        capture_output=True
    )
    return result.returncode == 0


def get_pane_id(session_name: str) -> str | None:
    """Get the pane ID for a session."""
    result = subprocess.run(
        ["tmux", "list-panes", "-t", session_name, "-F", "#{session_name}:#{window_index}.#{pane_index}"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return None
    lines = result.stdout.strip().split("\n")
    return lines[0] if lines else None


def start_worker_session(repo_path: str, task_name: str, description: str, topic_id: int) -> str | None:
    """Start a worker Claude session. Returns pane ID on success."""
    session_name = get_session_name(repo_path, task_name)
    worktree_path = get_worktree_path(repo_path, task_name)

    if session_exists(session_name):
        log(f"Worker session already exists: {session_name}")
        pane = get_pane_id(session_name)
        return pane

    if not worktree_path.exists():
        log(f"Worktree doesn't exist: {worktree_path}")
        return None

    log(f"Starting worker session: {session_name}")

    # Create tmux session in the worktree directory
    result = subprocess.run(
        ["tmux", "new-session", "-d", "-s", session_name, "-c", str(worktree_path)],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        log(f"Failed to create session: {result.stderr}")
        return None

    time.sleep(0.2)

    pane = get_pane_id(session_name)
    if not pane:
        log("Failed to get pane ID")
        return None

    # Write marker file
    marker_data = {
        "task_name": task_name,
        "repo": repo_path,
        "description": description,
        "topic_id": topic_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "active",
        "pane": pane
    }
    write_marker_file(str(worktree_path), marker_data)

    # Update registry
    registry = get_registry()
    registry.add_task(repo_path, task_name, {
        "topic_id": topic_id,
        "pane": pane,
        "status": "active"
    })

    # Start Claude with the task description
    subprocess.run(["tmux", "send-keys", "-t", pane, f'claude "{description}"', "Enter"])

    log(f"Worker session started: {pane}")
    return pane


def stop_worker_session(repo_path: str, task_name: str) -> bool:
    """Stop a worker session."""
    session_name = get_session_name(repo_path, task_name)

    if not session_exists(session_name):
        return True

    result = subprocess.run(
        ["tmux", "kill-session", "-t", session_name],
        capture_output=True
    )

    if result.returncode == 0:
        log(f"Worker session stopped: {session_name}")
        return True
    return False


def pause_worker(repo_path: str, task_name: str) -> bool:
    """Pause a worker (stop session, mark as paused)."""
    worktree_path = get_worktree_path(repo_path, task_name)
    marker = read_marker_file(str(worktree_path))

    if not marker:
        return False

    topic_id = marker.get("topic_id")

    # Stop session
    stop_worker_session(repo_path, task_name)

    # Update marker
    marker["status"] = "paused"
    write_marker_file(str(worktree_path), marker)

    # Update registry
    registry = get_registry()
    task_data = registry.get_task(repo_path, task_name)
    if task_data:
        task_data["status"] = "paused"
        registry.add_task(repo_path, task_name, task_data)

    # Update topic name
    if topic_id:
        update_topic_status(topic_id, task_name, "paused")

    log(f"Worker paused: {task_name}")
    return True


def resume_worker(repo_path: str, task_name: str) -> str | None:
    """Resume a paused worker. Returns pane ID on success."""
    worktree_path = get_worktree_path(repo_path, task_name)
    marker = read_marker_file(str(worktree_path))

    if not marker:
        return None

    topic_id = marker.get("topic_id")

    # Update marker
    marker["status"] = "active"
    write_marker_file(str(worktree_path), marker)

    # Start session with --resume
    session_name = get_session_name(repo_path, task_name)

    result = subprocess.run(
        ["tmux", "new-session", "-d", "-s", session_name, "-c", str(worktree_path)],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        log(f"Failed to create session: {result.stderr}")
        return None

    time.sleep(0.2)
    pane = get_pane_id(session_name)

    if pane:
        subprocess.run(["tmux", "send-keys", "-t", pane, "claude --resume", "Enter"])

        # Update registry
        registry = get_registry()
        registry.add_task(repo_path, task_name, {
            "topic_id": topic_id,
            "pane": pane,
            "status": "active"
        })

        # Update topic name
        if topic_id:
            update_topic_status(topic_id, task_name, "active")

        log(f"Worker resumed: {pane}")

    return pane


def cleanup_task(repo_path: str, task_name: str, delete_worktree_flag: bool = True) -> bool:
    """Clean up a completed task. Stops session, marks done, optionally deletes worktree."""
    worktree_path = get_worktree_path(repo_path, task_name)
    marker = read_marker_file(str(worktree_path))

    topic_id = marker.get("topic_id") if marker else None

    # Stop session
    stop_worker_session(repo_path, task_name)

    # Update topic name
    if topic_id:
        update_topic_status(topic_id, task_name, "done")

    # Remove from registry
    registry = get_registry()
    registry.remove_task(repo_path, task_name)

    # Delete worktree if requested
    if delete_worktree_flag:
        delete_worktree(repo_path, task_name)

    log(f"Task cleaned up: {task_name}")
    return True


def get_worker_pane_for_topic(topic_id: int) -> str | None:
    """Get the worker pane for a topic ID."""
    registry = get_registry()
    result = registry.find_task_by_topic(topic_id)
    if result:
        repo_path, task_name, task_data = result
        return task_data.get("pane")
    return None


def send_to_worker(topic_id: int, text: str) -> bool:
    """Send text to the worker handling a topic. Lazily resurrects if needed."""
    registry = get_registry()
    result = registry.find_task_by_topic(topic_id)
    if not result:
        log(f"No task for topic {topic_id}")
        return False

    repo_path, task_name, task_data = result

    # Check if session exists, resurrect if needed
    session_name = get_session_name(repo_path, task_name)
    pane = task_data.get("pane")

    if not pane or not session_exists(session_name):
        if task_data.get("status") == "paused":
            log(f"Worker {task_name} is paused, not resurrecting")
            return False
        pane = resume_worker(repo_path, task_name)

    if not pane:
        log(f"Failed to get worker pane for topic {topic_id}")
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
    for repo_path, task_name, task_data in registry.get_all_tasks():
        if task_data.get("pane") == pane:
            return True, task_data.get("topic_id")
    return False, None


def check_and_resurrect_worker(repo_path: str, task_name: str) -> str | None:
    """Check if worker session exists, resurrect if needed. Returns pane ID."""
    registry = get_registry()
    task_data = registry.get_task(repo_path, task_name)

    if not task_data:
        return None

    if task_data.get("status") == "paused":
        return None

    session_name = get_session_name(repo_path, task_name)
    if session_exists(session_name):
        pane = get_pane_id(session_name)
        if pane and pane != task_data.get("pane"):
            task_data["pane"] = pane
            registry.add_task(repo_path, task_name, task_data)
        return pane

    log(f"Worker session missing, resurrecting: {task_name}")
    return resume_worker(repo_path, task_name)
