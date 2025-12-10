"""Registry and configuration management for Claude Army."""

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from telegram_utils import log

CLAUDE_ARMY_DIR = Path(__file__).parent / "operator"
CONFIG_FILE = CLAUDE_ARMY_DIR / "config.json"
REGISTRY_FILE = CLAUDE_ARMY_DIR / "registry.json"
MARKER_FILE_NAME = "army.json"  # Lives inside .claude/ directory


def ensure_dir():
    """Ensure ~/.claude-army directory exists."""
    CLAUDE_ARMY_DIR.mkdir(exist_ok=True)


def _read_json(path: Path) -> dict | None:
    """Read JSON file. Returns None on error (vs {} for missing file)."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as e:
        log(f"JSON parse error in {path}: {e}")
        return None  # Distinguish parse error from missing file
    except IOError as e:
        log(f"IO error reading {path}: {e}")
        return None


def _write_json(path: Path, data: dict):
    """Write JSON file atomically (write to temp, then rename)."""
    ensure_dir()
    # Write to temp file in same directory (for atomic rename)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2)
        os.rename(tmp_path, path)
    except:
        # Clean up temp file on failure
        try:
            os.unlink(tmp_path)
        except:
            pass
        raise


# ============ ReloadableJSON base class ============

class ReloadableJSON:
    """Base class for JSON files that auto-reload on external modification.

    Subclasses must set self._path before calling super().__init__().
    """

    def __init__(self):
        self._cache = {}
        self._mtime = 0
        self._reload()

    def _reload(self) -> bool:
        """Read from disk. Returns True if successful."""
        data = _read_json(self._path)
        if data is not None:
            self._cache = data
            self._mtime = self._path.stat().st_mtime if self._path.exists() else 0
            return True
        return False

    def _maybe_reload(self):
        """Reload if file changed on disk."""
        try:
            mtime = self._path.stat().st_mtime if self._path.exists() else 0
            if mtime > self._mtime:
                self._reload()
        except OSError:
            pass

    @property
    def _data(self) -> dict:
        """Access data, checking for reload first."""
        self._maybe_reload()
        return self._cache

    def _flush(self):
        """Write to disk and update mtime."""
        _write_json(self._path, self._cache)
        try:
            self._mtime = self._path.stat().st_mtime
        except OSError:
            pass


# ============ Config (persistent settings) ============

class Config(ReloadableJSON):
    """Persistent configuration (bot settings, group ID, etc.)."""

    def __init__(self):
        self._path = CONFIG_FILE
        super().__init__()

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

    # Topic mappings (for crash recovery)
    def store_topic_mapping(self, topic_id: int, name: str):
        """Store topic_id -> name mapping (for crash recovery)."""
        mappings = self.get("topic_mappings", {})
        mappings[str(topic_id)] = name
        self.set("topic_mappings", mappings)

    def get_topic_name(self, topic_id: int) -> str | None:
        """Get topic name from mapping."""
        mappings = self.get("topic_mappings", {})
        return mappings.get(str(topic_id))

    def clear(self):
        """Clear all configuration."""
        self._cache = {}
        self._flush()


# ============ Registry (cache, rebuildable) ============

class Registry(ReloadableJSON):
    """Cache of tasks. Can be rebuilt from .claude/army.json marker files.

    Flat structure: tasks keyed by name, each with type, path, topic_id, etc.
    """

    def __init__(self):
        self._path = REGISTRY_FILE
        super().__init__()
        self._ensure_tasks_key()

    def _reload(self) -> bool:
        """Override to ensure tasks key exists after reload."""
        result = super()._reload()
        self._ensure_tasks_key()
        return result

    def _ensure_tasks_key(self):
        """Ensure cache has tasks dict."""
        if "tasks" not in self._cache:
            self._cache["tasks"] = {}

    @property
    def tasks(self) -> dict:
        """Get all tasks."""
        return self._data.get("tasks", {})

    def add_task(self, name: str, task_data: dict):
        """Add or update a task."""
        self._data["tasks"][name] = task_data
        self._flush()

    def get_task(self, name: str) -> dict | None:
        """Get task data by name."""
        return self._data["tasks"].get(name)

    def remove_task(self, name: str):
        """Remove a task from registry."""
        if name in self._data["tasks"]:
            del self._data["tasks"][name]
            self._flush()

    def get_all_tasks(self) -> list[tuple[str, dict]]:
        """Get all tasks as list of (name, task_data)."""
        return list(self._data["tasks"].items())

    def find_task_by_topic(self, topic_id: int) -> tuple[str, dict] | None:
        """Find task by topic ID."""
        for name, task_data in self._data["tasks"].items():
            if task_data.get("topic_id") == topic_id:
                return (name, task_data)
        return None

    def find_task_by_path(self, path: str) -> tuple[str, dict] | None:
        """Find task by directory path."""
        for name, task_data in self._data["tasks"].items():
            if task_data.get("path") == path:
                return (name, task_data)
        return None

    def find_task_by_pane(self, pane: str) -> tuple[str, dict] | None:
        """Find task by tmux pane."""
        for name, task_data in self._data["tasks"].items():
            if task_data.get("pane") == pane:
                return (name, task_data)
        return None

    def clear(self):
        """Clear all registry data."""
        self._cache = {"tasks": {}}
        self._flush()


# ============ Marker Files ============

def get_marker_path(directory: str) -> Path:
    """Get path to marker file (.claude/army.json) in a directory."""
    return Path(directory) / ".claude" / MARKER_FILE_NAME


def read_marker_file(directory: str) -> dict | None:
    """Read marker file from directory. Returns None if missing."""
    try:
        return json.loads(get_marker_path(directory).read_text())
    except FileNotFoundError:
        return None
    # Let JSONDecodeError propagate - corrupted marker is a real error


def write_marker_file(directory: str, data: dict):
    """Write marker file to directory. Creates .claude/ if needed."""
    marker_path = get_marker_path(directory)
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(json.dumps(data, indent=2))


def remove_marker_file(directory: str) -> bool:
    """Remove marker file from directory. Returns True if removed."""
    marker_path = get_marker_path(directory)
    if marker_path.exists():
        marker_path.unlink()
        return True
    return False


def is_managed_directory(directory: str) -> bool:
    """Check if directory has a Claude Army marker."""
    return get_marker_path(directory).exists()


# ============ Pending Marker Functions (crash recovery) ============

def write_marker_file_pending(directory: str, task_name: str):
    """Write pending marker before topic creation (crash recovery)."""
    from datetime import datetime, timezone
    data = {
        "pending_topic_name": task_name,
        "pending_since": datetime.now(timezone.utc).isoformat()
    }
    write_marker_file(directory, data)


def complete_pending_marker(directory: str, task_name: str, topic_id: int, task_type: str = "session"):
    """Complete a pending marker by adding topic_id and task metadata."""
    from datetime import datetime, timezone
    data = {
        "name": task_name,
        "type": task_type,
        "topic_id": topic_id,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    write_marker_file(directory, data)


def get_pending_markers() -> list[dict]:
    """Find all markers with pending_topic_name but no topic_id."""
    markers = scan_for_marker_files()
    return [m for m in markers if m.get("pending_topic_name") and not m.get("topic_id")]


def get_pending_marker_names() -> list[str]:
    """Get names of all pending markers."""
    return [m["pending_topic_name"] for m in get_pending_markers()]


def find_pending_marker_by_name(task_name: str) -> dict | None:
    """Find a pending marker by task name. Returns marker data with 'path' or None."""
    for marker in get_pending_markers():
        if marker.get("pending_topic_name") == task_name:
            return marker
    return None


def scan_for_marker_files(search_paths: list[str] = None) -> list[dict]:
    """Scan for .claude/army.json marker files to rebuild registry.

    Returns list of marker file contents with 'path' (directory) added.
    """
    import subprocess

    if search_paths is None:
        search_paths = [str(Path.home())]

    markers = []
    for search_path in search_paths:
        try:
            # Find army.json files inside .claude directories
            result = subprocess.run(
                ["find", search_path, "-path", "*/.claude/army.json", "-type", "f"],
                capture_output=True, text=True, timeout=30
            )
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                # army.json is at /path/to/dir/.claude/army.json
                # so directory is parent.parent
                directory = str(Path(line).parent.parent)
                marker_data = read_marker_file(directory)
                if marker_data:
                    marker_data["path"] = directory
                    markers.append(marker_data)
        except (subprocess.TimeoutExpired, Exception):
            continue

    return markers


# ============ Registry Recovery ============

def rebuild_registry_from_markers(search_paths: list[str] = None) -> int:
    """Rebuild registry by scanning for .claude/army.json marker files.

    Returns number of tasks recovered.
    """
    markers = scan_for_marker_files(search_paths)
    registry = get_registry()
    recovered = 0

    for marker in markers:
        name = marker.get("name")
        task_type = marker.get("type", "session")
        topic_id = marker.get("topic_id")
        path = marker.get("path")
        repo = marker.get("repo")  # Only for worktrees

        if not name or not path:
            continue

        # Check if already in registry
        existing = registry.get_task(name)
        if existing:
            continue

        # Add to registry
        task_data = {
            "type": task_type,
            "path": path,
            "topic_id": topic_id,
        }
        if repo:
            task_data["repo"] = repo

        registry.add_task(name, task_data)
        recovered += 1
        log(f"Recovered task: {name} ({task_type}) at {path}")

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
