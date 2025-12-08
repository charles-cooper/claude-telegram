"""Registry and configuration management for Claude Army."""

import json
from pathlib import Path
from typing import Any

CLAUDE_ARMY_DIR = Path(__file__).parent / "operator"
CONFIG_FILE = CLAUDE_ARMY_DIR / "config.json"
REGISTRY_FILE = CLAUDE_ARMY_DIR / "registry.json"
MARKER_FILE_NAME = ".claude-army-task"


def ensure_dir():
    """Ensure ~/.claude-army directory exists."""
    CLAUDE_ARMY_DIR.mkdir(exist_ok=True)


def _read_json(path: Path) -> dict:
    """Read JSON file, return empty dict if missing/invalid."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, IOError):
        return {}


def _write_json(path: Path, data: dict):
    """Write JSON file."""
    ensure_dir()
    path.write_text(json.dumps(data, indent=2))


# ============ Config (persistent settings) ============

class Config:
    """Persistent configuration (bot settings, group ID, etc.)."""

    def __init__(self):
        self._data = _read_json(CONFIG_FILE)

    def _flush(self):
        _write_json(CONFIG_FILE, self._data)

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any):
        self._data[key] = value
        self._flush()

    def delete(self, key: str):
        if key in self._data:
            del self._data[key]
            self._flush()

    @property
    def group_id(self) -> int | None:
        """The configured Telegram group ID."""
        return self._data.get("group_id")

    @group_id.setter
    def group_id(self, value: int):
        self.set("group_id", value)

    @property
    def general_topic_id(self) -> int | None:
        """The General topic ID for operator messages."""
        return self._data.get("general_topic_id")

    @general_topic_id.setter
    def general_topic_id(self, value: int):
        self.set("general_topic_id", value)

    @property
    def operator_pane(self) -> str | None:
        """The tmux pane for operator Claude."""
        return self._data.get("operator_pane")

    @operator_pane.setter
    def operator_pane(self, value: str):
        self.set("operator_pane", value)

    def is_configured(self) -> bool:
        """Check if Claude Army is configured."""
        return self.group_id is not None

    def clear(self):
        """Clear all configuration."""
        self._data = {}
        self._flush()


# ============ Registry (cache, rebuildable) ============

class Registry:
    """Cache of tasks and repos. Can be rebuilt from marker files."""

    def __init__(self):
        self._data = _read_json(REGISTRY_FILE)
        if "repos" not in self._data:
            self._data["repos"] = {}

    def _flush(self):
        _write_json(REGISTRY_FILE, self._data)

    @property
    def repos(self) -> dict:
        """Get all registered repos."""
        return self._data.get("repos", {})

    def add_repo(self, repo_path: str, worktree_base: str = "trees"):
        """Register a new repo."""
        if repo_path not in self._data["repos"]:
            self._data["repos"][repo_path] = {
                "worktree_base": worktree_base,
                "tasks": {}
            }
            self._flush()

    def remove_repo(self, repo_path: str):
        """Remove a repo from registry."""
        if repo_path in self._data["repos"]:
            del self._data["repos"][repo_path]
            self._flush()

    def add_task(self, repo_path: str, task_name: str, task_data: dict):
        """Add a task to a repo."""
        if repo_path not in self._data["repos"]:
            self.add_repo(repo_path)
        self._data["repos"][repo_path]["tasks"][task_name] = task_data
        self._flush()

    def get_task(self, repo_path: str, task_name: str) -> dict | None:
        """Get task data."""
        repo = self._data["repos"].get(repo_path)
        if not repo:
            return None
        return repo.get("tasks", {}).get(task_name)

    def remove_task(self, repo_path: str, task_name: str):
        """Remove a task from registry."""
        repo = self._data["repos"].get(repo_path)
        if repo and task_name in repo.get("tasks", {}):
            del repo["tasks"][task_name]
            self._flush()

    def get_all_tasks(self) -> list[tuple[str, str, dict]]:
        """Get all tasks as list of (repo_path, task_name, task_data)."""
        tasks = []
        for repo_path, repo_data in self._data["repos"].items():
            for task_name, task_data in repo_data.get("tasks", {}).items():
                tasks.append((repo_path, task_name, task_data))
        return tasks

    def find_task_by_topic(self, topic_id: int) -> tuple[str, str, dict] | None:
        """Find task by topic ID."""
        for repo_path, repo_data in self._data["repos"].items():
            for task_name, task_data in repo_data.get("tasks", {}).items():
                if task_data.get("topic_id") == topic_id:
                    return (repo_path, task_name, task_data)
        return None

    def clear(self):
        """Clear all registry data."""
        self._data = {"repos": {}}
        self._flush()


# ============ Marker Files ============

def get_marker_path(worktree_path: str) -> Path:
    """Get path to marker file in worktree."""
    return Path(worktree_path) / MARKER_FILE_NAME


def read_marker_file(worktree_path: str) -> dict | None:
    """Read marker file from worktree. Returns None if missing/invalid."""
    marker_path = get_marker_path(worktree_path)
    if not marker_path.exists():
        return None
    try:
        return json.loads(marker_path.read_text())
    except (json.JSONDecodeError, IOError):
        return None


def write_marker_file(worktree_path: str, data: dict):
    """Write marker file to worktree."""
    marker_path = get_marker_path(worktree_path)
    marker_path.write_text(json.dumps(data, indent=2))


def is_managed_worktree(worktree_path: str) -> bool:
    """Check if worktree is managed by Claude Army."""
    return get_marker_path(worktree_path).exists()


def scan_for_marker_files(search_paths: list[str] = None) -> list[dict]:
    """Scan for marker files to rebuild registry.

    Returns list of marker file contents with 'worktree_path' added.
    """
    import subprocess

    if search_paths is None:
        search_paths = [str(Path.home())]

    markers = []
    for search_path in search_paths:
        try:
            result = subprocess.run(
                ["find", search_path, "-name", MARKER_FILE_NAME, "-type", "f"],
                capture_output=True, text=True, timeout=30
            )
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                worktree_path = str(Path(line).parent)
                marker_data = read_marker_file(worktree_path)
                if marker_data:
                    marker_data["worktree_path"] = worktree_path
                    markers.append(marker_data)
        except (subprocess.TimeoutExpired, Exception):
            continue

    return markers


# ============ Registry Recovery ============

def rebuild_registry_from_markers(search_paths: list[str] = None) -> int:
    """Rebuild registry by scanning for marker files.

    Returns number of tasks recovered.
    """
    from telegram_utils import log

    markers = scan_for_marker_files(search_paths)
    registry = get_registry()
    recovered = 0

    for marker in markers:
        repo_path = marker.get("repo")
        task_name = marker.get("task_name")
        topic_id = marker.get("topic_id")
        status = marker.get("status", "active")
        worktree_path = marker.get("worktree_path")

        if not repo_path or not task_name:
            continue

        # Check if already in registry
        existing = registry.get_task(repo_path, task_name)
        if existing:
            continue

        # Add to registry
        registry.add_task(repo_path, task_name, {
            "topic_id": topic_id,
            "status": status,
            "worktree_path": worktree_path
        })
        recovered += 1
        log(f"Recovered task: {task_name} in {repo_path}")

    return recovered


# ============ Singleton instances ============

_config = None
_registry = None


def get_config() -> Config:
    """Get singleton Config instance."""
    global _config
    if _config is None:
        _config = Config()
    return _config


def get_registry() -> Registry:
    """Get singleton Registry instance."""
    global _registry
    if _registry is None:
        _registry = Registry()
    return _registry


def reset_singletons():
    """Reset singletons (for testing or after recovery)."""
    global _config, _registry
    _config = None
    _registry = None
