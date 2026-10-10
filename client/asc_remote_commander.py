#!/usr/bin/env python3
"""ASC Remote Commander: a self-hosted, stdio-only MCP agent (Python 3.10+).

No paid relays or platform API key. The agent connects through your own
ASC Remote Hub service using outbound-only TLS.
"""

from __future__ import annotations

import base64
import hashlib
import itertools
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

VERSION = "1.0.0"
MAX_INPUT = 2_000_000
MAX_TEXT = 100_000
MAX_FILE_WRITE = 256_000
MAX_PROCESS_READ = 100_000
PROCESS_TIMEOUT_LIMIT = 120


def enabled(name: str) -> bool:
    return os.environ.get(name, "").lower() in {"1", "yes", "true", "on"}


def configured_roots() -> list[Path]:
    raw = os.environ.get("ASC_RC_ROOTS", "")
    # A semicolon delimiter works on Windows and on POSIX.
    candidates = [p.strip().strip('"') for p in raw.split(";") if p.strip()] if raw else [str(Path.home())]
    result: list[Path] = []
    for s in candidates:
        p = Path(os.path.expandvars(os.path.expanduser(s))).resolve()
        if not p.is_dir():
            raise RuntimeError(f"Allowed root does not exist: {p}")
        result.append(p)
    if not result:
        raise RuntimeError("At least one existing allowed root is required")
    return result


class AccessDenied(Exception):
    pass


class Agent:
    def __init__(self) -> None:
        self.roots = configured_roots()
        self.allow_write = enabled("ASC_RC_ALLOW_WRITE")
        self.allow_exec = enabled("ASC_RC_ALLOW_EXEC")
        self.allow_gui = enabled("ASC_RC_ALLOW_GUI")
        self.allow_delete = enabled("ASC_RC_ALLOW_DELETE") and self.allow_write
        self.state_dir = Path(os.environ.get("ASC_RC_STATE_DIR", str(Path.home() / ".asc-remote-commander"))).resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, tuple[subprocess.Popen[bytes], Path, float]] = {}
        self._lock = threading.Lock()
        self.tools: dict[str, tuple[dict[str, Any], Callable[[dict[str, Any]], Any]]] = {}
        self._register_tools()

    def path(self, value: str, must_exist: bool = False) -> Path:
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ValueError("Absolute non-empty path required")
        p = Path(os.path.expandvars(os.path.expanduser(value)))
        if not p.is_absolute():
            raise ValueError("Use an absolute path")
        p = p.resolve(strict=False)
        if not any(p == root or root in p.parents for root in self.roots):
            raise AccessDenied("Path is outside ASC_RC_ROOTS")
        if must_exist and not p.exists():
            raise FileNotFoundError(str(p))
        return p

    def require(self, capability: str) -> None:
        if not getattr(self, "allow_" + capability):
            raise AccessDenied(f"Capability '{capability}' is disabled in agent settings")

    def audit(self, tool: str, args: dict[str, Any], status: str) -> None:
        # Never record command text, file contents, OAuth tokens or auth headers.
        digest = hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()[:16]
        record = {"time": datetime.now(timezone.utc).isoformat(), "tool": tool,
                  "args_sha256_prefix": digest, "status": status}
        try:
            with self._lock, (self.state_dir / "audit.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            print("audit-log write failed", file=sys.stderr)

    def _add(self, name: str, description: str, properties: dict[str, Any],
             func: Callable[[dict[str, Any]], Any], *, required: list[str] | None = None,
             read_only: bool = True, destructive: bool = False) -> None:
        schema = {"type": "object", "properties": properties, "additionalProperties": False}
        if required:
            schema["required"] = required
        self.tools[name] = ({"name": name, "description": description,
                             "inputSchema": schema,
                             "annotations": {"readOnlyHint": read_only, "destructiveHint": destructive,
                                             "openWorldHint": not read_only and name in {"run_command", "start_process"}}}, func)

    def _register_tools(self) -> None:
        S = lambda desc: {"type": "string", "description": desc}
        path_arg = S("Absolute path under ASC_RC_ROOTS")
        num = lambda desc, low=0, high=100000: {"type": "integer", "description": desc, "minimum": low, "maximum": high}

        self._add("machine_info", "Identify this connected computer, its operating system and granted capabilities. Does not read environment secrets.", {}, self.machine_info)
        self._add("list_directory", "List files and folders inside an allowed directory (max 200 entries).", {
            "path": path_arg, "limit": num("Number of entries", 1, 200)}, self.list_directory, required=["path"])
        self._add("stat_path", "Get size, type and modification time for an allowed file or directory.", {
            "path": path_arg}, self.stat_path, required=["path"])
        self._add("read_file", "Read a bounded range of UTF-8 text lines from an allowed file; reports next line offset.", {
            "path": path_arg, "offset": num("Zero-based first line"), "limit": num("Max lines", 1, 500)},
            self.read_file, required=["path"])
        self._add("search_files", "Find filenames matching a glob inside an allowed directory; does not follow symlinked directories.", {
            "path": path_arg, "pattern": S("Filename glob, for example *.py"),
            "limit": num("Max matches", 1, 200), "depth": num("Maximum directory depth", 0, 10)},
            self.search_files, required=["path", "pattern"])
        if self.allow_write:
            self._add("write_file", "Create/append/overwrite UTF-8 text under an allowed root. Overwrite destroys the old content. Maximum 256 KB per call.", {
                "path": path_arg, "text": S("UTF-8 text to write"),
                "mode": {"type": "string", "enum": ["create", "append", "overwrite"], "default": "create"}},
                self.write_file, required=["path", "text"], read_only=False, destructive=True)
            self._add("replace_text", "Replace an exact string in an existing UTF-8 file; fails unless match count equals expected_count.", {
                "path": path_arg, "old_text": S("Exact existing text"), "new_text": S("Replacement text"),
                "expected_count": num("Expected number of replacements", 1, 100)},
                self.replace_text, required=["path", "old_text", "new_text"], read_only=False, destructive=True)
            self._add("create_directory", "Create a directory inside an allowed root, including missing parents.", {
                "path": path_arg}, self.create_directory, required=["path"], read_only=False)
            self._add("move_path", "Move a file or directory between two allowed paths. Will not overwrite the destination.", {
                "source": path_arg, "destination": path_arg}, self.move_path,
                required=["source", "destination"], read_only=False)
        if self.allow_delete:
            self._add("delete_file", "Permanently remove one file (never a directory or symlink). Requires confirm='DELETE'.", {
                "path": path_arg, "confirm": {"type": "string", "enum": ["DELETE"]}},
                self.delete_file, required=["path", "confirm"], read_only=False, destructive=True)
        if self.allow_exec:
            self._add("run_command", "Run a local program WITHOUT a shell, wait up to 120 seconds, return exit code and bounded output. program + args are executed on the connected device. Commands can change files and access the network.", {
                "program": S("Executable name or path, e.g. python or powershell.exe"),
                "args": {"type": "array", "items": {"type": "string"}, "maxItems": 64},
                "cwd": path_arg, "timeout_seconds": num("Timeout", 1, PROCESS_TIMEOUT_LIMIT)},
                self.run_command, required=["program"], read_only=False, destructive=True)
            self._add("start_process", "Start a long-running local program without a shell; returns job ID for later reading/stopping.", {
                "program": S("Executable"), "args": {"type": "array", "items": {"type": "string"}, "maxItems": 64},
                "cwd": path_arg}, self.start_process, required=["program"], read_only=False, destructive=True)
            self._add("read_process", "Get bounded stdout/stderr and running status from a process started by this agent.", {
                "job_id": S("Job ID from start_process"), "offset": num("Byte offset"),
                "limit": num("Max bytes", 1, MAX_PROCESS_READ)},
                self.read_process, required=["job_id"])
            self._add("stop_process", "Terminate a job created in this agent session (never an arbitrary PID).", {
                "job_id": S("Job ID from start_process")}, self.stop_process,
                required=["job_id"], read_only=False, destructive=True)
        if self.allow_gui:
            self._add("screenshot", "Capture the current interactive desktop as a PNG image (may show personal information). Requires active graphical session and Pillow.", {
                "max_width": num("Downsample width", 320, 2400)}, self.screenshot)
            self._add("click_mouse", "Click within the interactive desktop. PyAutoGUI fail-safe is enabled.", {
                "x": num("Screen X", 0, 20000), "y": num("Screen Y", 0, 20000),
                "button": {"type": "string", "enum": ["left", "right", "middle"]},
                "clicks": num("Number of clicks", 1, 2)}, self.click_mouse,
                required=["x", "y"], read_only=False)
            self._add("type_text", "Type text into the focused window. On Windows, Unicode input is supported. This changes the active application.", {
                "text": S("Text, at most 4000 characters"), "interval_ms": num("Delay between characters", 0, 200)},
                self.type_text, required=["text"], read_only=False)
            self._add("press_keys", "Send a key or shortcut to the focused window using PyAutoGUI, e.g. ['ctrl','s'].", {
                "keys": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 5}},
                self.press_keys, required=["keys"], read_only=False)
            self._add("scroll_mouse", "Scroll at the cursor position; positive values scroll up.", {
                "clicks": num("Absolute number of notches", 1, 30),
                "direction": {"type": "string", "enum": ["up", "down"]}},
                self.scroll_mouse, required=["clicks", "direction"], read_only=False)

    def machine_info(self, a: dict[str, Any]) -> dict[str, Any]:
        return {"name": socket.gethostname(), "platform": platform.platform(), "python": platform.python_version(),
                "agent_version": VERSION, "roots": [str(x) for x in self.roots],
                "permissions": {"read": True, "write": self.allow_write, "exec": self.allow_exec,
                                "gui": self.allow_gui, "delete": self.allow_delete}}

    def list_directory(self, a: dict[str, Any]) -> dict[str, Any]:
        p = self.path(a["path"], True)
        limit = checked_int(a.get("limit", 100), 1, 200)
        if not p.is_dir():
            raise NotADirectoryError(str(p))
        entries = []
        with os.scandir(p) as it:
            for entry in itertools.islice(it, limit + 1):
                if len(entries) == limit:
                    return {"path": str(p), "entries": entries, "truncated": True}
                entries.append({"name": entry.name, "type": "directory" if entry.is_dir(follow_symlinks=False) else
                               "symlink" if entry.is_symlink() else "file"})
        return {"path": str(p), "entries": entries, "truncated": False}

    def stat_path(self, a: dict[str, Any]) -> dict[str, Any]:
        p = self.path(a["path"], True)
        s = p.stat()
        return {"path": str(p), "type": "directory" if p.is_dir() else "file",
                "size": s.st_size, "mtime_utc": datetime.fromtimestamp(s.st_mtime, timezone.utc).isoformat()}

    def read_file(self, a: dict[str, Any]) -> dict[str, Any]:
        p = self.path(a["path"], True)
        offset = checked_int(a.get("offset", 0), 0, 10_000_000)
        limit = checked_int(a.get("limit", 200), 1, 500)
        if p.stat().st_size > 100_000_000:
            raise ValueError("File exceeds 100 MB; use targeted command tools")
        with p.open("r", encoding="utf-8", errors="replace") as f:
            chunk = list(itertools.islice(f, offset, offset + limit + 1))
        has_more = len(chunk) > limit
        text = "".join(chunk[:limit])
        if "\x00" in text:
            raise ValueError("Binary file; use another local tool")
        if len(text) > MAX_TEXT:
            text = text[:MAX_TEXT]
            has_more = True
            # next line offset cannot be trusted after a mid-line truncation
            return {"path": str(p), "offset": offset, "text": text, "truncated_mid_line": True}
        return {"path": str(p), "offset": offset, "text": text,
                "next_offset": offset + min(len(chunk), limit) if has_more else None}

    def search_files(self, a: dict[str, Any]) -> dict[str, Any]:
        import fnmatch
        root = self.path(a["path"], True)
        pattern = str(a["pattern"])
        if not root.is_dir():
            raise NotADirectoryError(str(root))
        if not pattern or len(pattern) > 200 or "/" in pattern or "\\" in pattern:
            raise ValueError("Pattern must be a filename glob")
        maxdepth = checked_int(a.get("depth", 4), 0, 10)
        limit = checked_int(a.get("limit", 100), 1, 200)
        found: list[str] = []
        start = time.monotonic()
        for current, directories, files in os.walk(root, followlinks=False):
            if time.monotonic() - start > 7:
                return {"matches": found, "truncated": True, "reason": "search time limit"}
            level = len(Path(current).relative_to(root).parts)
            directories[:] = [d for d in directories if not Path(current, d).is_symlink()] if level < maxdepth else []
            for name in files + directories:
                if fnmatch.fnmatch(name, pattern):
                    p = Path(current) / name
                    # Refuse leaking filenames behind symlinks outside the root.
                    try:
                        self.path(str(p))
                    except AccessDenied:
                        continue
                    found.append(str(p))
                    if len(found) >= limit:
                        return {"matches": found, "truncated": True}
        return {"matches": found, "truncated": False}

    def _atomic_write(self, p: Path, text: str) -> None:
        data = text.encode("utf-8")
        if len(data) > 2_000_000:
            raise ValueError("File too large for atomic replacement")
        handle, tmp_name = tempfile.mkstemp(prefix=".asc-rc-", dir=str(p.parent))
        try:
            with os.fdopen(handle, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, p)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def write_file(self, a: dict[str, Any]) -> dict[str, Any]:
        self.require("write")
        p = self.path(a["path"])
        mode = a.get("mode", "create")
        text = a["text"]
        if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_FILE_WRITE:
            raise ValueError("Text must be UTF-8 and <= 256 KB")
        if mode == "create":
            with p.open("x", encoding="utf-8", newline="") as f:
                f.write(text)
        elif mode == "append":
            if not p.is_file():
                raise FileNotFoundError("Append only to existing files")
            with p.open("a", encoding="utf-8", newline="") as f:
                f.write(text)
        elif mode == "overwrite":
            if not p.is_file():
                raise FileNotFoundError("Overwrite only an existing file")
            self._atomic_write(p, text)
        else:
            raise ValueError("mode must be create, append, overwrite")
        return {"path": str(p), "bytes_written": len(text.encode("utf-8")), "mode": mode}

    def replace_text(self, a: dict[str, Any]) -> dict[str, Any]:
        self.require("write")
        p = self.path(a["path"], True)
        old, new = a["old_text"], a["new_text"]
        count = checked_int(a.get("expected_count", 1), 1, 100)
        if not isinstance(old, str) or not old or not isinstance(new, str):
            raise ValueError("old_text must be non-empty; new_text must be string")
        if p.stat().st_size > 2_000_000:
            raise ValueError("File exceeds 2 MB")
        original = p.read_text(encoding="utf-8")
        actual = original.count(old)
        if actual != count:
            raise ValueError(f"Expected {count} matches, found {actual}. File unchanged")
        self._atomic_write(p, original.replace(old, new))
        return {"path": str(p), "replaced": actual}

    def create_directory(self, a: dict[str, Any]) -> dict[str, Any]:
        self.require("write")
        p = self.path(a["path"])
        p.mkdir(parents=True, exist_ok=True)
        return {"path": str(p)}

    def move_path(self, a: dict[str, Any]) -> dict[str, Any]:
        self.require("write")
        # Moving a symlink would otherwise move its resolved target unexpectedly.
        if Path(a["source"]).is_symlink():
            raise ValueError("Move symlinks is disabled")
        src, dest = self.path(a["source"], True), self.path(a["destination"])
        if dest.exists():
            raise FileExistsError(str(dest))
        if src == dest or (src.is_dir() and src in dest.parents):
            raise ValueError("Invalid destination")
        # shutil.move may cross filesystems; both endpoints must be within allowed roots.
        shutil.move(str(src), str(dest))
        return {"source": str(src), "destination": str(dest)}

    def delete_file(self, a: dict[str, Any]) -> dict[str, Any]:
        self.require("delete")
        if a.get("confirm") != "DELETE":
            raise AccessDenied("Confirmation required")
        p = self.path(a["path"], True)
        if not p.is_file() or p.is_symlink():
            raise ValueError("Only regular files can be deleted")
        p.unlink()
        return {"deleted": str(p)}

    def _argv(self, a: dict[str, Any]) -> tuple[list[str], str]:
        program = a.get("program")
        argv = a.get("args", [])
        if not isinstance(program, str) or not program or "\x00" in program or len(program) > 1024:
            raise ValueError("Invalid program")
        if not isinstance(argv, list) or len(argv) > 64 or any(not isinstance(x, str) or "\x00" in x or len(x) > 20000 for x in argv):
            raise ValueError("args must contain up to 64 strings")
        cwd = str(self.path(a.get("cwd", str(self.roots[0])), True))
        if not Path(cwd).is_dir():
            raise NotADirectoryError(cwd)
        # Explicitly shell=False. Shell interpreters are still available if enabled.
        return [program] + argv, cwd

    def run_command(self, a: dict[str, Any]) -> dict[str, Any]:
        self.require("exec")
        argv, cwd = self._argv(a)
        timeout = checked_int(a.get("timeout_seconds", 30), 1, PROCESS_TIMEOUT_LIMIT)
        # Disk-backed capture avoids holding unbounded child output in RAM.
        with tempfile.TemporaryFile(mode="w+b") as output:
            proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                                    stdout=output, stderr=subprocess.STDOUT, shell=False)
            timed_out = False
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                proc.kill()
                proc.wait()
            output.flush()
            total = output.tell()
            output.seek(0)
            out = output.read(MAX_TEXT).decode("utf-8", errors="replace")
            return {"exit_code": proc.returncode, "output": out, "truncated": total > MAX_TEXT,
                    "timed_out": timed_out}

    def start_process(self, a: dict[str, Any]) -> dict[str, Any]:
        self.require("exec")
        argv, cwd = self._argv(a)
        job_id = uuid.uuid4().hex
        log = self.state_dir / ("job-" + job_id + ".log")
        with log.open("wb") as f:
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=f,
                                    stderr=subprocess.STDOUT, shell=False,
                                    creationflags=creationflags, start_new_session=(os.name != "nt"))
        self._jobs[job_id] = (proc, log, time.time())
        return {"job_id": job_id, "pid": proc.pid, "started": True}

    def _job(self, a: dict[str, Any]) -> tuple[subprocess.Popen[bytes], Path, float]:
        job_id = a.get("job_id", "")
        if not isinstance(job_id, str) or job_id not in self._jobs:
            raise ValueError("Unknown job_id (jobs belong to the current agent session)")
        return self._jobs[job_id]

    def read_process(self, a: dict[str, Any]) -> dict[str, Any]:
        proc, log, started = self._job(a)
        offset = checked_int(a.get("offset", 0), 0, 1_000_000_000)
        limit = checked_int(a.get("limit", 32_000), 1, MAX_PROCESS_READ)
        with log.open("rb") as f:
            f.seek(offset)
            data = f.read(limit)
            next_offset = f.tell()
        return {"running": proc.poll() is None, "exit_code": proc.poll(), "output": data.decode("utf-8", errors="replace"),
                "next_offset": next_offset, "log_size": log.stat().st_size}

    def stop_process(self, a: dict[str, Any]) -> dict[str, Any]:
        self.require("exec")
        proc, _, _ = self._job(a)
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=4)
        return {"stopped": True, "exit_code": proc.returncode}

    def screenshot(self, a: dict[str, Any]) -> dict[str, Any]:
        self.require("gui")
        try:
            from PIL import ImageGrab
            import io
        except ImportError as e:
            raise RuntimeError("Install GUI dependencies: pip install -r requirements-gui.txt") from e
        im = ImageGrab.grab(all_screens=True)
        max_width = checked_int(a.get("max_width", 1600), 320, 2400)
        if im.width > max_width:
            im.thumbnail((max_width, int(im.height * max_width / im.width)))
        memory = io.BytesIO()
        im.save(memory, format="PNG", optimize=True)
        if len(memory.getvalue()) > 4_000_000:
            raise ValueError("Screenshot exceeds 4 MB; reduce max_width")
        return {"_image": base64.b64encode(memory.getvalue()).decode("ascii"),
                "width": im.width, "height": im.height}

    def _gui(self):
        self.require("gui")
        try:
            import pyautogui
        except (ImportError, KeyError, OSError) as e:
            raise RuntimeError("GUI needs an interactive desktop and requirements-gui.txt") from e
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.15
        return pyautogui

    def click_mouse(self, a: dict[str, Any]) -> dict[str, Any]:
        gui = self._gui()
        x, y = checked_int(a["x"], 0, 20000), checked_int(a["y"], 0, 20000)
        if not 0 <= x < gui.size().width or not 0 <= y < gui.size().height:
            raise ValueError("Coordinates outside primary display")
        button = a.get("button", "left")
        if button not in ("left", "right", "middle"):
            raise ValueError("Invalid button")
        gui.click(x, y, clicks=checked_int(a.get("clicks", 1), 1, 2), button=button)
        return {"clicked": [x, y], "button": button}

    def type_text(self, a: dict[str, Any]) -> dict[str, Any]:
        gui = self._gui()
        text = a["text"]
        if not isinstance(text, str) or len(text) > 4000:
            raise ValueError("Max 4000 characters")
        interval = checked_int(a.get("interval_ms", 0), 0, 200) / 1000
        if os.name == "nt" and not text.isascii():
            # Windows Unicode input via keyboard without changing clipboard.
            windows_unicode_type(text, interval)
        elif text.isascii():
            gui.write(text, interval=interval)
        else:
            # On Linux use clipboard paste so Cyrillic/Unicode works.
            try:
                import pyperclip
                previous = pyperclip.paste()
                pyperclip.copy(text)
                gui.hotkey("ctrl", "v")
                time.sleep(0.1)
                pyperclip.copy(previous)
            except ImportError as e:
                raise RuntimeError("Install pyperclip for Unicode typing on Linux") from e
        return {"typed_characters": len(text)}

    def press_keys(self, a: dict[str, Any]) -> dict[str, Any]:
        gui = self._gui()
        keys = a["keys"]
        if not isinstance(keys, list) or not 1 <= len(keys) <= 5 or any(not isinstance(k, str) or k not in gui.KEYBOARD_KEYS for k in keys):
            raise ValueError("Unknown keyboard key")
        if len(keys) == 1:
            gui.press(keys[0])
        else:
            gui.hotkey(*keys)
        return {"keys": keys}

    def scroll_mouse(self, a: dict[str, Any]) -> dict[str, Any]:
        gui = self._gui()
        clicks = checked_int(a["clicks"], 1, 30)
        direction = a["direction"]
        if direction not in ("up", "down"):
            raise ValueError("Invalid scroll direction")
        gui.scroll(clicks if direction == "up" else -clicks)
        return {"scrolled": clicks, "direction": direction}

    def handle_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in self.tools:
            return {"content": [{"type": "text", "text": f"Unknown or disabled tool: {name}"}], "isError": True}
        try:
            if not isinstance(arguments, dict):
                raise ValueError("Tool arguments must be a JSON object")
            result = self.tools[name][1](arguments)
            self.audit(name, arguments, "ok")
            if isinstance(result, dict) and "_image" in result:
                return {"content": [{"type": "text", "text": json.dumps({k: v for k, v in result.items() if k != "_image"})},
                                    {"type": "image", "mimeType": "image/png", "data": result["_image"]}], "isError": False}
            return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, default=str)}], "isError": False}
        except Exception as e:
            self.audit(name, arguments if isinstance(arguments, dict) else {}, type(e).__name__)
            # Don't leak full OS traces or secrets; error types and safe messages suffice.
            message = str(e)[:400]
            return {"content": [{"type": "text", "text": f"{type(e).__name__}: {message}"}], "isError": True}


def checked_int(x: Any, low: int, high: int) -> int:
    if isinstance(x, bool) or not isinstance(x, int) or not low <= x <= high:
        raise ValueError(f"Expected integer in range {low}..{high}")
    return x


def windows_input_events(text: str) -> list[tuple[int,int,int]]:
    """Generate Windows keyboard events, preserving Unicode and real line breaks.

    KEYEVENTF_UNICODE alone does not map CR/LF to an Enter key in many editors.
    Keyboard control characters must use their real virtual key codes.
    The function has no side effects and can be tested on Linux or Windows.
    """
    events: list[tuple[int,int,int]] = []
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    for char in normalized:
        if char in ("\n","\t"):
            keycode = 0x0D if char == "\n" else 0x09  # VK_RETURN, VK_TAB
            events.append((keycode,0,0))
            events.append((keycode,0,0x0002))
        else:
            data = char.encode("utf-16-le")
            for i in range(0,len(data),2):
                unit = int.from_bytes(data[i:i+2],"little")
                events.append((0,unit,0x0004))
                events.append((0,unit,0x0004|0x0002))
    return events


def windows_unicode_type(s: str, interval: float) -> None:
    # https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-sendinput
    import ctypes
    from ctypes import wintypes
    ULONG_PTR = wintypes.WPARAM
    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ULONG_PTR)]
    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]
    class INPUT_U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]
    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", INPUT_U)]
    for virtual_key, scan_code, flags in windows_input_events(s):
        record = INPUT(type=1, ki=KEYBDINPUT(virtual_key, scan_code, flags, 0, 0))
        if ctypes.windll.user32.SendInput(1, ctypes.byref(record), ctypes.sizeof(INPUT)) != 1:
            raise RuntimeError("Windows SendInput failed")
        if interval and not (flags & 0x0002):  # delay once per key press
            time.sleep(interval)


def dispatch(agent: Agent, request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    rid = request.get("id")
    if rid is None:
        return None  # notifications
    try:
        if method == "initialize":
            params = request.get("params") or {}
            requested = params.get("protocolVersion", "2025-06-18")
            supported = {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}
            result = {"protocolVersion": requested if requested in supported else "2025-06-18",
                      "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "asc-remote-commander", "version": VERSION},
                      "instructions": "These tools operate on the user's own computer. Verify machine_info before changes. Ask for confirmation before destructive actions, system changes, or sending private data outside the computer. Use bounded file reads and review results."}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [x[0] for x in agent.tools.values()]}
        elif method == "tools/call":
            p = request.get("params") or {}
            result = agent.handle_tool(p.get("name", ""), p.get("arguments") or {})
        else:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}}
        return {"jsonrpc": "2.0", "id": rid, "result": result}
    except Exception as e:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": type(e).__name__}}


def main() -> int:
    try:
        agent = Agent()
    except Exception as e:
        print(f"ASC Remote Commander configuration error: {e}", file=sys.stderr)
        return 2
    # MCP stdio: stdout MUST ONLY contain JSON-RPC frames, one JSON object per line.
    reader = sys.stdin.buffer
    writer = sys.stdout.buffer
    while True:
        raw = reader.readline(MAX_INPUT + 1)
        if not raw:
            return 0
        if len(raw) > MAX_INPUT:
            while raw and not raw.endswith(b"\n"):
                raw = reader.readline(MAX_INPUT + 1)
            print("MCP request rejected: frame too long", file=sys.stderr)
            continue
        try:
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise ValueError("request must be object")
            response = dispatch(agent, request)
            if response is not None:
                writer.write(json.dumps(response, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n")
                writer.flush()
        except Exception as e:
            # Invalid input is not trusted; don't let a bad request kill the agent.
            print("MCP invalid frame: " + type(e).__name__, file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
