"""Transcript watcher - monitors Claude transcripts for permission prompts."""

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from telegram_utils import pane_exists, log

# Delay before notifying - allows tool_result to arrive for auto-accepted tools
NOTIFY_DELAY = 0.4  # seconds

# Tools that should never notify (internal/auto-approved tools)
SKIP_TOOLS = {"BashOutput", "KillShell", "AgentOutputTool", "TodoWrite"}


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
class CompactionEvent:
    """A context compaction event."""
    trigger: str  # "auto" or "manual"
    pre_tokens: int
    pane: str
    cwd: str


@dataclass
class IdleEvent:
    """Claude finished speaking and is waiting for input."""
    text: str
    pane: str
    cwd: str
    transcript_path: str = ""
    msg_id: str = ""  # Claude message ID for tracking supersession


@dataclass
class ActivityInfo:
    """Info about an active session (for typing indicator)."""
    pane: str
    cwd: str


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
    tool_queue: list = field(default_factory=list)  # Ordered queue of tool_ids (for batched tool calls)
    compactions: list = field(default_factory=list)  # Compaction events (notify immediately)
    idle_events: list = field(default_factory=list)  # Idle events (notify immediately)
    last_check: float = 0
    # Track last notified message to avoid duplicate idle notifications
    last_idle_msg_id: str = ""
    # Track message IDs that have tool_use (for supersession detection)
    tool_use_msg_ids: set = field(default_factory=set)

    def check(self) -> tuple[list[PendingTool], list[CompactionEvent], list[IdleEvent], bool]:
        """Check for new pending tools, compactions, idle events, and activity.

        Returns (pending_tools, compactions, idle_events, had_activity).
        had_activity is True if any new content was found in transcript.
        """
        # Read new lines and update state
        had_activity = False
        try:
            with open(self.path, 'r') as f:
                f.seek(self.position)
                for line in f:
                    self._process_line(line)
                    had_activity = True  # Any new content = activity
                self.position = f.tell()
        except FileNotFoundError:
            pass
        except Exception as e:
            log(f"Error reading {self.path}: {e}")
        self.last_check = time.time()

        # Get compaction and idle events
        compactions = self.compactions
        self.compactions = []
        idle_events = self.idle_events
        self.idle_events = []

        # Clean up completed tools from queue, pending, and notified
        self.tool_queue = [t for t in self.tool_queue if t not in self.tool_results]
        for tool_id in list(self.pending_tools.keys()):
            if tool_id in self.tool_results:
                del self.pending_tools[tool_id]
        self.notified_tools -= self.tool_results  # Clear notified status when result arrives

        # Find current tool (first in queue without result)
        # Only notify if no earlier tool is still waiting for result
        ready_tools = []
        now = time.time()
        for tool_id in self.tool_queue:
            if tool_id in self.tool_results:
                continue  # Has result, skip
            if tool_id in self.notified_tools:
                # Already notified, waiting for result - BLOCK until it completes
                break
            if tool_id not in self.pending_tools:
                continue  # Not pending (shouldn't happen)
            tool = self.pending_tools[tool_id]
            if now - tool.detected_at > NOTIFY_DELAY:
                ready_tools.append(tool)
                self.notified_tools.add(tool_id)
                del self.pending_tools[tool_id]
                break  # Only return one tool at a time

        return ready_tools, compactions, idle_events, had_activity

    def _handle_compaction(self, entry: dict) -> bool:
        """Handle compaction event. Returns True if handled."""
        if entry.get("type") != "system" or entry.get("subtype") != "compact_boundary":
            return False
        metadata = entry.get("compactMetadata", {})
        log(f"Detected: compaction ({metadata.get('trigger', 'unknown')})")
        self.compactions.append(CompactionEvent(
            trigger=metadata.get("trigger", "unknown"),
            pre_tokens=metadata.get("preTokens", 0),
            pane=self.pane,
            cwd=self.cwd
        ))
        return True

    def _handle_tool_result(self, entry: dict) -> bool:
        """Handle tool_result entries. Returns True if handled."""
        if entry.get("type") != "user":
            return False
        for c in entry.get("message", {}).get("content", []):
            if isinstance(c, dict) and c.get("type") == "tool_result":
                tool_use_id = c.get("tool_use_id")
                if tool_use_id:
                    self.tool_results.add(tool_use_id)
                    self.notified_tools.discard(tool_use_id)
                    self.pending_tools.pop(tool_use_id, None)
        return True

    def _process_line(self, line: str) -> bool:
        """Process a single transcript line. Returns True if Claude is actively working."""
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            return False  # Partial line

        # Handle compaction events
        if self._handle_compaction(entry):
            return False  # Compaction isn't "active work"

        # Track tool_results
        if self._handle_tool_result(entry):
            return False  # Tool results mean Claude is waiting, not working

        # Only assistant entries from here on
        if entry.get("type") != "assistant":
            return False

        message = entry.get("message", {})
        msg_id = message.get("id", "")
        assistant_text = ""
        tool_calls = []
        has_thinking = False

        # Collect ALL tool_use from this message (Claude can batch them)
        for c in message.get("content", []):
            if isinstance(c, dict):
                if c.get("type") == "text":
                    assistant_text = c.get("text", "")
                elif c.get("type") == "tool_use":
                    tool_calls.append(c)
                elif c.get("type") == "thinking":
                    has_thinking = True

        # Thinking block = Claude is actively working
        if has_thinking and not tool_calls and not assistant_text:
            return True  # Trigger typing indicator

        # If we see tool_use, mark that this message is not idle
        if tool_calls and msg_id:
            self.tool_use_msg_ids.add(msg_id)
            # Clear any pending idle for this message
            if self.last_idle_msg_id == msg_id:
                self.last_idle_msg_id = ""  # Will be handled by daemon via supersession

        # Check for idle event: assistant text with no tool_use
        if assistant_text and not tool_calls and msg_id:
            # Only notify once per message
            if msg_id != self.last_idle_msg_id:
                log(f"Detected: idle (text-only message)")
                self.idle_events.append(IdleEvent(
                    text=assistant_text,
                    pane=self.pane,
                    cwd=self.cwd,
                    transcript_path=self.path,
                    msg_id=msg_id
                ))
                self.last_idle_msg_id = msg_id
                return True  # Trigger typing - will be cancelled by idle message
            return False

        if not tool_calls:
            return False

        # Process each tool_use in order (TUI shows them sequentially)
        for tool_call in tool_calls:
            tool_id = tool_call.get("id", "")
            tool_name = tool_call.get("name", "")

            # Skip internal tools that are always auto-approved
            if tool_name in SKIP_TOOLS:
                continue

            # Skip if already notified or already has result or already pending
            if tool_id in self.notified_tools or tool_id in self.tool_results:
                continue
            if tool_id in self.pending_tools:
                continue

            # Add to queue and pending - will notify in order after delay
            log(f"Detected: {tool_name} ({tool_id[:20]}...)")
            self.tool_queue.append(tool_id)
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

        return True  # tool_use = Claude is actively working


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

            # Scan for existing tool_results so we can expire stale notifications
            existing_results = set()
            try:
                with open(transcript_path, 'r') as f:
                    for line in f:
                        try:
                            entry_data = json.loads(line)
                            if entry_data.get("type") == "user":
                                for c in entry_data.get("message", {}).get("content", []):
                                    if isinstance(c, dict) and c.get("type") == "tool_result":
                                        tool_use_id = c.get("tool_use_id")
                                        if tool_use_id:
                                            existing_results.add(tool_use_id)
                        except json.JSONDecodeError:
                            continue
            except Exception as e:
                log(f"Error scanning transcript for tool_results: {e}")

            watcher = TranscriptWatcher(
                path=transcript_path,
                pane=pane,
                cwd=cwd,
                position=size
            )
            watcher.tool_results = existing_results
            self.watchers[transcript_path] = watcher
            self.pane_to_transcript[pane] = transcript_path
            log(f"Watching transcript (from state): {transcript_path} (pane {pane}, cwd {cwd}, {len(existing_results)} existing results)")

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

    def check_all(self) -> tuple[list[PendingTool], list[CompactionEvent], list[IdleEvent], list[ActivityInfo]]:
        """Check all watchers for pending tools, compactions, idle events, and activity."""
        all_tools = []
        all_compactions = []
        all_idle = []
        all_activity = []
        for watcher in self.watchers.values():
            tools, compactions, idle_events, had_activity = watcher.check()
            all_tools.extend(tools)
            all_compactions.extend(compactions)
            all_idle.extend(idle_events)
            if had_activity:
                all_activity.append(ActivityInfo(pane=watcher.pane, cwd=watcher.cwd))
        return all_tools, all_compactions, all_idle, all_activity
