"""Transcript watcher - monitors Claude transcripts for permission prompts."""

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from telegram_utils import pane_exists, log

# Delay before notifying - allows tool_result to arrive for auto-accepted tools
NOTIFY_DELAY = 0.2  # seconds


@dataclass
class PendingTool:
    """A tool_use waiting for permission."""
    tool_id: str
    tool_name: str
    tool_input: dict
    assistant_text: str
    transcript_path: str
    pane: str
    cwd: str
    detected_at: float = 0  # timestamp when detected


@dataclass
class TranscriptWatcher:
    """Watches a single transcript file for new tool_use entries."""
    path: str
    pane: str
    cwd: str  # Store cwd from discovery
    position: int = 0
    notified_tools: set = field(default_factory=set)
    tool_results: set = field(default_factory=set)
    pending_tools: dict = field(default_factory=dict)  # tool_id -> PendingTool
    last_check: float = 0

    def check(self) -> list[PendingTool]:
        """Check for new pending tools. Returns list of tools ready to notify."""
        # First, read new lines and update state
        try:
            with open(self.path, 'r') as f:
                f.seek(self.position)
                for line in f:
                    self._process_line(line)
                self.position = f.tell()
        except FileNotFoundError:
            pass
        except Exception as e:
            log(f"Error reading {self.path}: {e}")
        self.last_check = time.time()

        # Now check which pending tools are ready to notify
        ready = []
        now = time.time()
        done = []
        for tool_id, tool in self.pending_tools.items():
            # If tool_result arrived, don't notify
            if tool_id in self.tool_results:
                done.append(tool_id)
                continue
            # If enough time passed without tool_result, notify
            if now - tool.detected_at > NOTIFY_DELAY:
                ready.append(tool)
                self.notified_tools.add(tool_id)
                done.append(tool_id)

        for tool_id in done:
            del self.pending_tools[tool_id]

        return ready

    def _process_line(self, line: str):
        """Process a single transcript line."""
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            return  # Partial line

        # Track tool_results
        if entry.get("type") == "user":
            for c in entry.get("message", {}).get("content", []):
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    tool_use_id = c.get("tool_use_id")
                    if tool_use_id:
                        self.tool_results.add(tool_use_id)
                        self.notified_tools.discard(tool_use_id)
                        # Remove from pending if waiting
                        if tool_use_id in self.pending_tools:
                            del self.pending_tools[tool_use_id]

        # Check for new tool_use
        if entry.get("type") != "assistant":
            return

        assistant_text = ""
        tool_call = None

        for c in entry.get("message", {}).get("content", []):
            if isinstance(c, dict):
                if c.get("type") == "text":
                    assistant_text = c.get("text", "")
                elif c.get("type") == "tool_use":
                    tool_call = c

        if not tool_call:
            return

        tool_id = tool_call.get("id", "")
        tool_name = tool_call.get("name", "")

        # Skip if already notified or already has result or already pending
        if tool_id in self.notified_tools or tool_id in self.tool_results:
            return
        if tool_id in self.pending_tools:
            return

        # Add to pending - will notify after delay if no tool_result arrives
        log(f"Detected: {tool_name} ({tool_id[:20]}...)")
        self.pending_tools[tool_id] = PendingTool(
            tool_id=tool_id,
            tool_name=tool_name,
            tool_input=tool_call.get("input", {}),
            assistant_text=assistant_text,
            transcript_path=self.path,
            pane=self.pane,
            cwd=self.cwd,
            detected_at=time.time()
        )


def decode_cwd_from_path(transcript_path: str) -> str:
    """Extract cwd from transcript path.

    Path format: ~/.claude/projects/{encoded-path}/{session}.jsonl
    Encoded path uses - for / (e.g., -home-ubuntu-myproject)
    """
    parts = transcript_path.split("/")
    for i, p in enumerate(parts):
        if p == "projects" and i + 1 < len(parts):
            encoded = parts[i + 1]
            # The encoding is: /home/ubuntu/foo -> -home-ubuntu-foo
            # So we replace leading - with / and internal - with /
            # But we need to be careful: -home-ubuntu-my-project
            # should become /home/ubuntu/my-project (hyphens in names preserved)
            # Actually Claude uses a different encoding... let's just return the encoded form
            # for now and get the real cwd from state
            return "/" + encoded.replace("-", "/", 3)  # Only first 3 dashes
    return ""


class TranscriptManager:
    """Manages multiple transcript watchers."""

    def __init__(self):
        self.watchers: dict[str, TranscriptWatcher] = {}  # path -> watcher
        self.pane_to_transcript: dict[str, str] = {}  # pane -> transcript path

    def discover_transcripts(self):
        """Find active transcripts from tmux panes running claude."""
        import glob as glob_module
        try:
            result = os.popen("tmux list-panes -a -F '#{session_name}:#{window_index}.#{pane_index} #{pane_current_path}'").read()
        except:
            return

        for line in result.strip().split("\n"):
            if not line:
                continue
            parts = line.split(" ", 1)
            if len(parts) != 2:
                continue
            pane, cwd = parts

            # Find transcript for this cwd
            # Claude encodes /home/ubuntu/foo as -home-ubuntu-foo
            encoded = cwd.replace("/", "-")
            pattern = str(Path.home() / f".claude/projects/{encoded}/*.jsonl")

            transcripts = sorted(
                [Path(p) for p in glob_module.glob(pattern)],
                key=lambda p: p.stat().st_mtime,
                reverse=True
            )

            if not transcripts:
                continue

            # Use most recently modified transcript
            transcript_path = str(transcripts[0])

            if transcript_path not in self.watchers:
                # Start watching from end of file
                try:
                    size = os.path.getsize(transcript_path)
                except:
                    size = 0
                self.watchers[transcript_path] = TranscriptWatcher(
                    path=transcript_path,
                    pane=pane,
                    cwd=cwd,  # Use actual cwd from tmux
                    position=size
                )
                log(f"Watching transcript: {transcript_path} (pane {pane}, cwd {cwd})")

            self.pane_to_transcript[pane] = transcript_path

    def add_from_state(self, state: dict):
        """Add watchers for transcripts mentioned in state file."""
        for msg_id, entry in state.items():
            transcript_path = entry.get("transcript_path")
            pane = entry.get("pane")
            cwd = entry.get("cwd", "")
            if not transcript_path or not pane:
                continue
            if transcript_path in self.watchers:
                continue
            if not Path(transcript_path).exists():
                continue

            # If no cwd in state, try to decode from path
            if not cwd:
                cwd = decode_cwd_from_path(transcript_path)

            # Start watching from end (we already notified for earlier entries)
            try:
                size = os.path.getsize(transcript_path)
            except:
                size = 0
            self.watchers[transcript_path] = TranscriptWatcher(
                path=transcript_path,
                pane=pane,
                cwd=cwd,
                position=size
            )
            self.pane_to_transcript[pane] = transcript_path
            log(f"Watching transcript (from state): {transcript_path} (pane {pane}, cwd {cwd})")

    def cleanup_dead(self):
        """Remove watchers for dead panes."""
        dead = []
        for path, watcher in self.watchers.items():
            if not pane_exists(watcher.pane):
                dead.append(path)
        for path in dead:
            pane = self.watchers[path].pane
            del self.watchers[path]
            if pane in self.pane_to_transcript:
                del self.pane_to_transcript[pane]
            log(f"Stopped watching (pane dead): {path}")

    def check_all(self) -> list[PendingTool]:
        """Check all watchers for pending tools."""
        all_pending = []
        for watcher in self.watchers.values():
            all_pending.extend(watcher.check())
        return all_pending
