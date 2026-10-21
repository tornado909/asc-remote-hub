#!/usr/bin/env python3
"""ASC Remote Commander: a self-hosted, stdio-only MCP agent (Python 3.10+).

No paid relays or platform API key. The agent connects through your own
ASC Remote Hub service using outbound-only TLS.
"""

from __future__ import annotations

import base64
import fnmatch
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

VERSION = "1.1.4"
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
        # A separate local opt-in for experimental, user-consented Game Agent.
        self.allow_game_agent = enabled("ASC_RC_ALLOW_GAME_AGENT")
        self._game_agent_backend = None
        # GUI capability does NOT imply permission to hijack the user's active
        # desktop. This flag must be set LOCALLY in the agent environment.
        self.allow_interactive_gui = enabled("ASC_RC_ALLOW_INTERACTIVE_GUI")
        # UIA providers may activate themselves even without SendInput.
        # Refuse mutations while the user is actively typing/moving their mouse.
        self.uia_active_user_override = enabled("ASC_RC_UIA_ALLOW_ACTIVE_USER")
        self._uia_focus_interference: set[tuple[int,int]] = set()
        self.allow_delete = enabled("ASC_RC_ALLOW_DELETE") and self.allow_write
        self.state_dir = Path(os.environ.get("ASC_RC_STATE_DIR", str(Path.home() / ".asc-remote-commander"))).resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, tuple[subprocess.Popen[bytes], Path, float]] = {}
        self._isolated_desktops = None
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
        # Bounded, root-confined batched reads amortise MCP round-trips.
        self._add("read_files", "Read up to 16 bounded UTF-8 file ranges in one call; stops at 100 KB aggregate.",
                  {"files":{"type":"array","minItems":1,"maxItems":16,
                   "items":{"type":"object","additionalProperties":False,
                            "properties":{"path":path_arg,"offset":num("First line",0,100000),
                                          "limit":num("Number of lines",1,500)},"required":["path"]}}},
                  self.read_files, required=["files"])
        self._add("stat_paths", "Stat up to 64 allowed files/directories at once.",
                  {"paths":{"type":"array","minItems":1,"maxItems":64,"items":path_arg}},
                  self.stat_paths, required=["paths"])
        self._add("directory_tree", "Bounded recursive directory listing (no symlink traversal; max 250 entries).",
                  {"path":path_arg,"depth":num("Maximum depth",0,5),
                   "limit":num("Maximum entries",1,250)},self.directory_tree,required=["path"])
        self._add("search_text", "Literal text search in bounded UTF-8 project files. No regular-expression execution.",
                  {"path":path_arg,"query":S("Literal search string"),
                   "pattern":S("Filename glob, e.g. *.py"),"depth":num("Directory depth",0,8),
                   "limit":num("Maximum matches",1,100),
                   "case_sensitive":{"type":"boolean"}},
                  self.search_text,required=["path","query"])
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
            if os.name == "nt":
                self._add("window_controls",
                          "Read UI Automation controls inside ONE verified Windows window without moving the mouse or stealing focus. Choose a unique automation_id/name for window_action. Browser support varies.",
                          {"hwnd":num("Target HWND from window_list",1,9223372036854775807),
                           "expected_pid":num("Target PID from window_list",1,4294967295),
                           "title_contains":S("Required stable title fragment, >=3 chars"),
                           "limit":num("Max controls returned",1,150),
                           "depth":num("Max control-tree depth",1,9)},
                          self.window_controls,
                          required=["hwnd","expected_pid","title_contains"])
                self._add("window_action",
                          "TARGETED UIA (no mouse/keyboard injection). May cause some apps to focus themselves; refuses actions while user actively works (<45 sec idle), and remembers windows that steal focus. Validates exact HWND/PID/title and one matching control. Unsupported actions fail; NO fallback to shared desktop.",
                          {"hwnd":num("Target HWND from window_list",1,9223372036854775807),
                           "expected_pid":num("Target PID from window_list",1,4294967295),
                           "title_contains":S("Required stable title fragment, >=3 chars"),
                           "selector":{"type":"object","additionalProperties":False,
                                       "properties":{"automation_id":S("Exact unique UIA AutomationId"),
                                                     "name":S("Exact UIA control name"),
                                                     "control_type":S("Expected type, e.g. Button, Edit"),
                                                     "class_name":S("Optional exact class name")}},
                           "action":{"type":"string","enum":["invoke","set_value","toggle","select","expand","collapse"]},
                           "value":S("Only for set_value; max 4000 chars")},
                          self.window_action,
                          required=["hwnd","expected_pid","title_contains","selector","action"],
                          read_only=False)
                if self.allow_exec:
                    # Strictly process-owned Win32 Desktops. No shared mouse,
                    # no SendInput, no VM, no arbitrary-window targets.
                    desktop_id=S("Opaque desktop_id returned by gui_desktop_create")
                    self._add("gui_desktop_create",
                              "Create an isolated Win32 Desktop for GUI applications inside the CURRENT Windows session. No VM. No cursor/focus change; limited to 3 per agent. Security isolation NOT guaranteed.",
                              {},self.gui_desktop_create,read_only=False)
                    self._add("gui_desktop_sessions",
                              "List only the isolated Desktops owned by this agent and their process IDs.",
                              {},self.gui_desktop_sessions)
                    self._add("gui_desktop_launch",
                              "Launch an executable onto an agent-owned isolated desktop with native CreateProcessW(lpDesktop), in a Windows Job Object; no shell or VM. Requires absolute program, returns owned PID.",
                              {"desktop_id":desktop_id,"program":S("Existing absolute EXE path"),
                               "args":{"type":"array","items":{"type":"string"},"maxItems":19},
                               "cwd":S("Optional existing absolute working directory")},
                              self.gui_desktop_launch,required=["desktop_id","program"],
                              read_only=False)
                    self._add("gui_desktop_windows",
                              "Enumerate top-level and child Win32 windows belonging ONLY to processes launched inside the given isolated Desktop.",
                              {"desktop_id":desktop_id,
                               "with_children":{"type":"boolean"},
                               "limit":num("Max windows",1,100)},
                              self.gui_desktop_windows,required=["desktop_id"])
                    self._add("gui_desktop_action",
                              "Address a standard Edit or Button control belonging to an owned isolated-desktop process. set_text uses WM_SETTEXT with verification; click_button uses BM_CLICK. NEVER moves physical mouse or sends global keystrokes.",
                              {"desktop_id":desktop_id,
                               "hwnd":num("Window handle from gui_desktop_windows",1,9223372036854775807),
                               "expected_pid":num("Exact PID from gui_desktop_windows",1,4294967295),
                               "method":{"type":"string","enum":["set_text","click_button"]},
                               "text":S("Required for set_text; max 4000 chars"),
                               "expected_class":S("Optional exact window class from gui_desktop_windows")},
                              self.gui_desktop_action,
                              required=["desktop_id","hwnd","expected_pid","method"],
                              read_only=False)
                    self._add("gui_desktop_capture",
                              "Capture ONLY an owned isolated-desktop window via PrintWindow. Returns PNG base64 plus rendered/blank flags; GPU surfaces can fail (never claims success from a black image).",
                              {"desktop_id":desktop_id,
                               "hwnd":num("Window handle",1,9223372036854775807),
                               "expected_pid":num("Owned window PID",1,4294967295),
                               "max_width":num("Capture width",320,1920)},
                              self.gui_desktop_capture,
                              required=["desktop_id","hwnd","expected_pid"])
                    self._add("gui_desktop_close",
                              "Terminate the owned process tree through a kill-on-close Windows Job and close its isolated Desktop. No other desktop/process affected.",
                              {"desktop_id":desktop_id},self.gui_desktop_close,
                              required=["desktop_id"],read_only=False,
                              destructive=True)
                if self.allow_exec and self.allow_game_agent:
                    # Optional research extension. Native v18/v19 are per-user,
                    # installed independently, and never bundled in the Hub.
                    target_identity={
                        "hwnd":S("Exact handle returned by game_agent_targets"),
                        "pid":num("Exact PID returned by game_agent_targets",1,4294967295),
                        "process_start_ticks":num("Process creation FILETIME returned by game_agent_targets",1,9223372036854775807)
                    }
                    self._add("game_agent_status",
                              "Verify installed ASC Game Agent v18/v19 manifests. No window scan, GPU use or capture.",
                              {},self.game_agent_status)
                    self._add("game_agent_targets",
                              "List locally eligible user-session Win32 windows via ASC Game Agent v18. Metadata ONLY; never captures pixels.",
                              {"limit":num("Maximum returned target windows",1,100)},self.game_agent_targets)
                    self._add("game_agent_preflight",
                              "Verify exact HWND, PID and process-start fingerprint with v18/v19 before asking for consent. No capture or GPU.",
                              target_identity,self.game_agent_preflight,
                              required=["hwnd","pid","process_start_ticks"])
                    self._add("game_agent_capture",
                              "Capture up to 32 frames from ONE selected HWND via GPU-only WGC->DirectML. Requires local ASC_RC_ALLOW_GAME_AGENT_CAPTURE=1 AND a native Windows YES click for this request. Cannot auto-approve, send input or save video. Returns COCO metadata only.",
                              {**target_identity,"frames":num("2..32 frames",2,32)},
                              self.game_agent_capture,required=["hwnd","pid","process_start_ticks"],
                              read_only=False)
                    self._add("game_agent_gpu_selftest",
                              "On-demand WGC/DirectML test of a TEMPORARY window created by the native test worker. Needs local ASC_RC_ALLOW_GAME_AGENT_GPU_TEST=1. Never captures third-party windows.",
                              {"frames":num("2..32 frames",2,32)},
                              self.game_agent_gpu_selftest,read_only=False)
                self._add("window_list", "Enumerate visible titled windows in the current Windows desktop (HWND, PID, bounds, foreground).",
                          {"limit":num("Max windows",1,100)},self.window_list)
                self._add("window_focus", "INTERACTIVE fallback: forcibly foregrounds a verified window and can disturb the user. Requires explicit allow_desktop_interference=true. Prefer background window_action.",
                           {"hwnd":num("HWND from window_list",1,9223372036854775807),
                            "expected_pid":num("Expected PID",1,4294967295),
                            "title_contains":S("Required title substring"),
                            "allow_desktop_interference":{"type":"boolean"}},
                           self.window_focus,
                           required=["hwnd","expected_pid","title_contains","allow_desktop_interference"],
                           read_only=False)
                self._add("window_screenshot", "Read-only screen crop of the window bounds. Can show OVERLAYING WINDOWS if target is occluded. Prefer window_controls for reliable background targeting.",
                          {"hwnd":num("HWND from window_list",1,9223372036854775807),
                           "max_width":num("Downsample width",320,2400)},
                          self.window_screenshot,required=["hwnd"])
                self._add("terminal_send", "INTERACTIVE only. Sends a line to a verified ALREADY-FOREGROUND terminal. Requires allow_desktop_interference=true. Refuses focus stealing; prefer run_command for CLI tasks.",
                           {"hwnd":num("HWND from window_list",1,9223372036854775807),
                            "expected_pid":num("Expected PID",1,4294967295),
                            "title_contains":S("Required window title fragment"),
                            "text":S("Single-line prompt, max 2000 characters"),
                            "submit":{"type":"boolean"},
                            "allow_desktop_interference":{"type":"boolean"}},
                           self.terminal_send,
                           required=["hwnd","expected_pid","title_contains","text","allow_desktop_interference"],
                           read_only=False)
            self._add("screenshot", "Capture the current interactive desktop as a PNG image (may show personal information). Requires active graphical session and Pillow.", {
                "max_width": num("Downsample width", 320, 2400)}, self.screenshot)
            # Shared desktop injection is NOT safe by default. It requires explicit
            # per-call opt-in and exact target identity, even under full GUI access.
            if os.name == "nt":
                identity={"hwnd":num("HWND from window_list",1,9223372036854775807),
                          "expected_pid":num("Expected PID",1,4294967295),
                          "title_contains":S("Expected title fragment (>=3 characters)"),
                          "allow_desktop_interference":{"type":"boolean",
                              "description":"Explicitly authorize shared user mouse/keyboard interference for THIS call."}}
                self._add("click_mouse",
                          "INTERACTIVE LEGACY FALLBACK only. Physically moves the user's mouse. Disabled without exact already-foreground HWND/PID/title and allow_desktop_interference=true. Prefer window_action.",
                          {**identity,"x":num("Screen X",-32768,32767),
                           "y":num("Screen Y",-32768,32767),
                           "button":{"type":"string","enum":["left","right","middle"]},
                           "clicks":num("Clicks",1,2)},self.click_mouse,
                          required=["hwnd","expected_pid","title_contains","allow_desktop_interference","x","y"],
                          read_only=False)
                self._add("type_text",
                          "INTERACTIVE LEGACY FALLBACK only, sends keys to verified already-foreground window. Disabled without explicit interference opt-in. Prefer window_action(set_value) for background text.",
                          {**identity,"text":S("Max 4000 characters"),
                           "interval_ms":num("Delay between characters",0,200)},
                          self.type_text,
                          required=["hwnd","expected_pid","title_contains","allow_desktop_interference","text"],
                          read_only=False)
                self._add("press_keys",
                          "INTERACTIVE LEGACY FALLBACK only, sends shared keyboard shortcut to verified already-foreground window. Requires explicit interference opt-in.",
                          {**identity,"keys":{"type":"array","items":{"type":"string"},"minItems":1,"maxItems":5}},
                          self.press_keys,
                          required=["hwnd","expected_pid","title_contains","allow_desktop_interference","keys"],
                          read_only=False)
                self._add("scroll_mouse",
                          "INTERACTIVE LEGACY FALLBACK only. Scrolls verified already-foreground window if mouse is already over it; explicit interference opt-in required.",
                          {**identity,"clicks":num("Scroll notches",1,30),
                           "direction":{"type":"string","enum":["up","down"]}},
                          self.scroll_mouse,
                          required=["hwnd","expected_pid","title_contains","allow_desktop_interference","clicks","direction"],
                          read_only=False)

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

    def read_files(self, a: dict[str, Any]) -> dict[str, Any]:
        files=a.get("files")
        if not isinstance(files,list) or not 1<=len(files)<=16:
            raise ValueError("files must be a list with 1-16 items")
        output=[]
        used=0
        for item in files:
            if not isinstance(item,dict) or set(item)-{"path","offset","limit"}:
                raise ValueError("Invalid read_files item")
            chunk=self.read_file(item)
            encoded=len(chunk["text"].encode("utf-8"))
            if output and used+encoded>MAX_TEXT:
                return {"files":output,"truncated":True,"reason":"Aggregate 100 KB limit"}
            if encoded>MAX_TEXT:
                # read_file has its own mid-line cap.
                chunk["text"]=chunk["text"].encode("utf-8")[:MAX_TEXT].decode("utf-8",errors="ignore")
                chunk["truncated_mid_line"]=True
                output.append(chunk)
                return {"files":output,"truncated":True,"reason":"Aggregate 100 KB limit"}
            output.append(chunk)
            used+=encoded
        return {"files":output,"truncated":False}

    def stat_paths(self, a: dict[str, Any]) -> dict[str, Any]:
        paths=a.get("paths")
        if not isinstance(paths,list) or not 1<=len(paths)<=64:
            raise ValueError("paths must have 1-64 entries")
        return {"entries":[self.stat_path({"path":p}) for p in paths]}

    def directory_tree(self, a: dict[str, Any]) -> dict[str, Any]:
        root=self.path(a["path"],True)
        if not root.is_dir():
            raise NotADirectoryError(str(root))
        depth=checked_int(a.get("depth",2),0,5)
        limit=checked_int(a.get("limit",150),1,250)
        entries=[]
        start=time.monotonic()
        for current,dirs,files in os.walk(root,followlinks=False):
            level=len(Path(current).relative_to(root).parts)
            dirs[:]=sorted(d for d in dirs
                           if not Path(current,d).is_symlink()) if level<depth else []
            for name in sorted(dirs+files):
                p=Path(current)/name
                if p.is_symlink(): continue
                try:self.path(str(p))
                except AccessDenied:continue
                entries.append({"path":str(p),"type":"directory" if p.is_dir() else "file"})
                if len(entries)>=limit or time.monotonic()-start>5:
                    return {"entries":entries,"truncated":True}
            if level>=depth:dirs[:]=[]
        return {"entries":entries,"truncated":False}

    def search_text(self, a: dict[str, Any]) -> dict[str, Any]:
        root=self.path(a["path"],True)
        if not root.is_dir(): raise NotADirectoryError(str(root))
        query=a.get("query")
        if not isinstance(query,str) or not 1<=len(query)<=200 or chr(0) in query:
            raise ValueError("query must contain 1-200 characters")
        pattern=a.get("pattern","*")
        if not isinstance(pattern,str) or not pattern or "/" in pattern or "\\\\" in pattern or len(pattern)>100:
            raise ValueError("pattern must be a filename glob")
        depth=checked_int(a.get("depth",4),0,8)
        limit=checked_int(a.get("limit",50),1,100)
        sensitive=a.get("case_sensitive",False)
        if not isinstance(sensitive,bool): raise ValueError("case_sensitive must be boolean")
        needle=query if sensitive else query.casefold()
        start=time.monotonic()
        scanned=0
        consumed=0
        matches=[]
        for current,dirs,files in os.walk(root,followlinks=False):
            level=len(Path(current).relative_to(root).parts)
            dirs[:]=sorted(d for d in dirs if d not in
                {".git","node_modules",".venv","venv","__pycache__",".cache","bin","obj"}
                and not Path(current,d).is_symlink()) if level<depth else []
            for name in sorted(files):
                if not fnmatch.fnmatch(name,pattern):continue
                p=Path(current)/name
                if p.is_symlink():continue
                try:
                    self.path(str(p))
                    size=p.stat().st_size
                    if size>1_000_000 or consumed+size>16_000_000:continue
                    scanned+=1
                    if scanned>600:
                        return {"matches":matches,"files_scanned":scanned-1,"truncated":True,"reason":"file_limit"}
                    consumed+=size
                    with p.open("r",encoding="utf-8-sig",errors="replace") as stream:
                        for n,line in enumerate(stream,1):
                            if n>100000:break
                            haystack=line if sensitive else line.casefold()
                            if needle in haystack:
                                matches.append({"path":str(p),"line":n,"text":line.rstrip()[:240]})
                                if len(matches)>=limit:
                                    return {"matches":matches,"files_scanned":scanned,
                                            "truncated":True,"reason":"match_limit"}
                except (OSError,UnicodeError):
                    continue
                if time.monotonic()-start>5:
                    return {"matches":matches,"files_scanned":scanned,"truncated":True,"reason":"time_limit"}
            if level>=depth:dirs[:]=[]
        return {"matches":matches,"files_scanned":scanned,"truncated":False}

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

    def _isolated(self):
        """Create one manager lazily; it persists as long as the bridge agent."""
        self.require("gui")
        self.require("exec")
        if os.name!="nt":
            raise RuntimeError("Isolated Win32 Desktops require Windows")
        if self._isolated_desktops is None:
            import atexit
            from asc_isolated_desktop import IsolatedDesktops
            self._isolated_desktops=IsolatedDesktops()
            atexit.register(self._isolated_desktops.close_all)
        return self._isolated_desktops

    def gui_desktop_create(self,a:dict[str,Any])->dict[str,Any]:
        return self._isolated().create()

    def gui_desktop_sessions(self,a:dict[str,Any])->dict[str,Any]:
        return self._isolated().sessions()

    def gui_desktop_launch(self,a:dict[str,Any])->dict[str,Any]:
        program=a.get("program")
        if not isinstance(program,str) or not os.path.isabs(program):
            raise ValueError("An absolute executable path is required")
        arguments=a.get("args",[])
        if not isinstance(arguments,list):
            raise ValueError("args must be a list")
        cwd=a.get("cwd")
        if cwd is not None and not isinstance(cwd,str):
            raise ValueError("cwd must be a string")
        return self._isolated().launch(
            a["desktop_id"],[program]+arguments,cwd)

    def gui_desktop_windows(self,a:dict[str,Any])->dict[str,Any]:
        children=a.get("with_children",True)
        if not isinstance(children,bool):
            raise ValueError("with_children must be boolean")
        limit=checked_int(a.get("limit",80),1,100)
        return self._isolated().windows(a["desktop_id"],children,limit)

    def gui_desktop_action(self,a:dict[str,Any])->dict[str,Any]:
        return self._isolated().action(
            a["desktop_id"],
            checked_int(a.get("hwnd"),1,9223372036854775807),
            checked_int(a.get("expected_pid"),1,4294967295),
            a.get("method"),text=a.get("text"),
            expected_class=a.get("expected_class"))

    def gui_desktop_capture(self,a:dict[str,Any])->dict[str,Any]:
        return self._isolated().capture(
            a["desktop_id"],
            checked_int(a.get("hwnd"),1,9223372036854775807),
            checked_int(a.get("expected_pid"),1,4294967295),
            checked_int(a.get("max_width",1000),320,1920))

    def gui_desktop_close(self,a:dict[str,Any])->dict[str,Any]:
        return self._isolated().close(a["desktop_id"])

    def _windows(self):
        self.require("gui")
        if os.name!="nt":
            raise RuntimeError("Windows interactive desktop required")
        import ctypes
        from ctypes import wintypes
        user32=ctypes.windll.user32
        # Native handles are pointer-sized, not Python's default C int:
        # without explicit prototypes HWND can silently truncate on x64.
        user32.IsWindow.argtypes=[wintypes.HWND]
        user32.IsWindow.restype=wintypes.BOOL
        user32.IsWindowVisible.argtypes=[wintypes.HWND]
        user32.IsWindowVisible.restype=wintypes.BOOL
        user32.GetWindowTextLengthW.argtypes=[wintypes.HWND]
        user32.GetWindowTextLengthW.restype=ctypes.c_int
        user32.GetWindowTextW.argtypes=[wintypes.HWND,ctypes.c_wchar_p,ctypes.c_int]
        user32.GetWindowTextW.restype=ctypes.c_int
        user32.GetWindowThreadProcessId.argtypes=[
            wintypes.HWND,ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype=wintypes.DWORD
        user32.GetWindowRect.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.RECT)]
        user32.GetWindowRect.restype=wintypes.BOOL
        user32.GetForegroundWindow.restype=wintypes.HWND
        user32.SetForegroundWindow.argtypes=[wintypes.HWND]
        user32.SetForegroundWindow.restype=wintypes.BOOL
        user32.GetCursorPos.argtypes=[ctypes.POINTER(wintypes.POINT)]
        user32.GetCursorPos.restype=wintypes.BOOL
        return ctypes,wintypes,user32

    def _window_details(self, hwnd: int) -> dict[str, Any]:
        ctypes,wintypes,user32=self._windows()
        if not isinstance(hwnd,int) or hwnd<=0 or not user32.IsWindow(hwnd):
            raise ValueError("Window HWND does not exist")
        length=user32.GetWindowTextLengthW(hwnd)
        title=ctypes.create_unicode_buffer(max(1,length+1))
        user32.GetWindowTextW(hwnd,title,len(title))
        pid=wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd,ctypes.byref(pid))
        rect=wintypes.RECT()
        if not user32.GetWindowRect(hwnd,ctypes.byref(rect)):
            raise RuntimeError("Cannot read window coordinates")
        return {"hwnd":hwnd,"pid":pid.value,"title":title.value,
                "visible":bool(user32.IsWindowVisible(hwnd)),
                "foreground":int(user32.GetForegroundWindow())==hwnd,
                "rect":{"left":rect.left,"top":rect.top,"right":rect.right,"bottom":rect.bottom}}

    def _game_agent(self):
        self.require("exec")
        self.require("gui")
        if os.name != "nt" or not self.allow_game_agent:
            raise AccessDenied("Game Agent requires Windows and local ASC_RC_ALLOW_GAME_AGENT=1")
        if self._game_agent_backend is None:
            from asc_game_agent import GameAgentBridge
            self._game_agent_backend = GameAgentBridge()
        return self._game_agent_backend

    def game_agent_status(self,a:dict[str,Any])->dict[str,Any]:
        return self._game_agent().status()

    def game_agent_targets(self,a:dict[str,Any])->dict[str,Any]:
        return self._game_agent().targets(checked_int(a.get("limit",50),1,100))

    def game_agent_preflight(self,a:dict[str,Any])->dict[str,Any]:
        return self._game_agent().preflight(a.get("hwnd"),a.get("pid"),a.get("process_start_ticks"))

    def game_agent_capture(self,a:dict[str,Any])->dict[str,Any]:
        return self._game_agent().capture(a.get("hwnd"),a.get("pid"),
                                           a.get("process_start_ticks"),
                                           checked_int(a.get("frames",12),2,32))

    def game_agent_gpu_selftest(self,a:dict[str,Any])->dict[str,Any]:
        return self._game_agent().fixture(checked_int(a.get("frames",8),2,32))

    def window_list(self,a:dict[str,Any])->dict[str,Any]:
        ctypes,wintypes,user32=self._windows()
        limit=checked_int(a.get("limit",50),1,100)
        windows=[]
        # Windows uses pointer-sized HWND; callbacks must remain referenced.
        callback=ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.HWND,wintypes.LPARAM)
        def visit(hwnd,lparam):
            try:
                if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd)>0:
                    win=self._window_details(int(hwnd))
                    if win["title"]:windows.append(win)
                return 0 if len(windows)>=limit else 1
            except (ValueError,RuntimeError):
                return 1
        cb=callback(visit)
        user32.EnumWindows(cb,0)
        return {"windows":windows,"truncated":len(windows)>=limit}

    def _target_window(self, a: dict[str, Any], *, foreground: bool = False) -> dict[str, Any]:
        """Verify HWND+PID+title of one specific Windows window, never guessing by focus."""
        self.require("gui")
        hwnd=checked_int(a.get("hwnd"),1,9223372036854775807)
        pid=checked_int(a.get("expected_pid"),1,4294967295)
        title=a.get("title_contains")
        if not isinstance(title,str) or len(title.strip())<3:
            raise ValueError("title_contains must be a stable fragment of >=3 characters")
        item=self._window_details(hwnd)
        rect=item["rect"]
        if item["pid"]!=pid or title.casefold() not in item["title"].casefold():
            raise ValueError("Target window identity changed; refresh window_list")
        if not item["visible"] or rect["right"]<=rect["left"] or rect["bottom"]<=rect["top"]:
            raise ValueError("Target window is not visible")
        if foreground and not item["foreground"]:
            raise AccessDenied("Target is not foreground; refusing keyboard/mouse injection. Use window_action for background control.")
        return item

    def _interactive_target(self, a: dict[str, Any], *, foreground: bool = True) -> dict[str, Any]:
        # TWO independent gates: a locally-administered setting plus deliberate
        # per-call opt-in. An LLM cannot enable physical desktop control alone.
        if not self.allow_interactive_gui:
            raise AccessDenied("Interactive desktop control is disabled by this agent's local policy (ASC_RC_ALLOW_INTERACTIVE_GUI=0); window_controls is always safe to inspect.")
        if a.get("allow_desktop_interference") is not True:
            raise AccessDenied("Shared-desktop mouse/keyboard injection is disabled by default. Use window_controls/window_action (background), or explicitly set allow_desktop_interference=true with HWND, expected_pid and title_contains.")
        return self._target_window(a,foreground=foreground)

    def _desktop_idle_seconds(self) -> float:
        """Local Windows session last-input timer; no touch/keyboard injection."""
        ctypes,wintypes,user32=self._windows()
        class LASTINPUTINFO(ctypes.Structure):
            _fields_=[("cbSize",wintypes.UINT),("dwTime",wintypes.DWORD)]
        info=LASTINPUTINFO()
        info.cbSize=ctypes.sizeof(info)
        user32.GetLastInputInfo.argtypes=[ctypes.POINTER(LASTINPUTINFO)]
        user32.GetLastInputInfo.restype=wintypes.BOOL
        if not user32.GetLastInputInfo(ctypes.byref(info)):
            raise RuntimeError("Cannot verify Windows user-idle state")
        ticks=ctypes.windll.kernel32.GetTickCount() & 0xffffffff
        elapsed=(ticks-int(info.dwTime)) & 0xffffffff
        return elapsed/1000.0

    def _require_desktop_idle(self) -> int:
        """Reject UIA mutations while the local person is using this session."""
        seconds=0 if self.uia_active_user_override else 45
        if seconds and self._desktop_idle_seconds()<seconds:
            raise AccessDenied("UserActive: background UIA action refused because the human is using the desktop. No key or mouse event was injected.")
        return seconds

    def _windows_uia(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Call the signed-by-package PowerShell UIA backend, never invoking SendInput."""
        self.require("gui")
        if os.name!="nt":
            raise RuntimeError("Background Windows UI Automation is Windows-only")
        script=Path(__file__).resolve().with_name("windows_uia.ps1")
        if not script.is_file():
            raise RuntimeError("Windows UI Automation backend is missing from the agent installation")
        data=json.dumps(payload,ensure_ascii=False,separators=(",",":")).encode("utf-8")
        if len(data)>16000:
            raise ValueError("UI Automation request exceeds 16 KB")
        try:
            proc=subprocess.run(
                ["powershell.exe","-NoLogo","-NoProfile","-NonInteractive",
                 "-ExecutionPolicy","Bypass","-File",str(script)],
                input=data,capture_output=True,timeout=19,check=False,
                creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        except subprocess.TimeoutExpired:
            raise RuntimeError("UIA_TIMEOUT: target provider did not respond within 19 seconds") from None
        if proc.returncode!=0 or not proc.stdout:
            raise RuntimeError("UIA_BACKEND_UNAVAILABLE: PowerShell UI Automation failed")
        try:
            output=json.loads(proc.stdout.decode("utf-8-sig"))
        except (UnicodeError,ValueError) as exc:
            raise RuntimeError("UIA_BACKEND_INVALID_JSON") from exc
        if not isinstance(output,dict) or output.get("ok") is not True:
            error=output.get("error","UIA_PROVIDER_ERROR") if isinstance(output,dict) else "UIA_PROVIDER_ERROR"
            raise RuntimeError("UIA_"+str(error)[:90])
        return output

    def window_controls(self,a:dict[str,Any])->dict[str,Any]:
        item=self._target_window(a)
        limit=checked_int(a.get("limit",90),1,150)
        depth=checked_int(a.get("depth",6),1,9)
        return self._windows_uia({
            "mode":"list","hwnd":item["hwnd"],"expected_pid":item["pid"],
            "title_contains":a["title_contains"],"limit":limit,"depth":depth
        })

    def window_action(self,a:dict[str,Any])->dict[str,Any]:
        item=self._target_window(a)
        if (item["hwnd"],item["pid"]) in self._uia_focus_interference:
            raise AccessDenied("UIA provider for this window previously stole focus; refusing further shared-desktop UIA actions")
        selector=a.get("selector")
        if not isinstance(selector,dict) or not (
            isinstance(selector.get("automation_id"),str) and selector.get("automation_id").strip() or
            isinstance(selector.get("name"),str) and selector.get("name").strip()):
            raise ValueError("A unique UIA selector automation_id or exact name is required")
        fields=("automation_id","name","control_type","class_name")
        if set(selector)-set(fields) or any(not isinstance(value,str) for value in selector.values()):
            raise ValueError("Invalid UIA selector fields")
        action=a.get("action")
        if action not in ("invoke","set_value","toggle","select","expand","collapse"):
            raise ValueError("Unknown UIA action")
        if action=="set_value":
            value=a.get("value")
            if not isinstance(value,str) or len(value)>4000:
                raise ValueError("set_value requires text of at most 4000 characters")
        elif a.get("value") is not None:
            raise ValueError("Only set_value supports a value")
        # Do idle preflight only for a valid action, never for a rejected request.
        min_idle=self._require_desktop_idle()
        result=self._windows_uia({
            "mode":"action","hwnd":item["hwnd"],"expected_pid":item["pid"],
            "title_contains":a["title_contains"],
            "selector":{key:selector[key] for key in fields if key in selector},
            "action":action,"min_idle_seconds":min_idle,
            **({"value":a["value"]} if action=="set_value" else {})
        })
        if result.get("foreground_unchanged") is not True:
            self._uia_focus_interference.add((item["hwnd"],item["pid"]))
            raise RuntimeError("UIA_FOCUS_INTERFERENCE: target app changed user foreground. Window blocked for further UIA actions in this agent session.")
        return result

    def window_focus(self,a:dict[str,Any])->dict[str,Any]:
        item=self._interactive_target(a,foreground=False)
        ctypes,wintypes,user32=self._windows()
        hwnd=item["hwnd"]
        if not item["foreground"]:
            # Do not spoof Alt / send shared keyboard keys to bypass OS focus rules.
            user32.SetForegroundWindow(hwnd)
        fresh=self._target_window(a,foreground=True)
        return {"hwnd":hwnd,"pid":fresh["pid"],"title":fresh["title"],
                "foreground":True,"desktop_interference":True}

    def window_screenshot(self,a:dict[str,Any])->dict[str,Any]:
        self.require("gui")
        from PIL import ImageGrab
        import io
        hwnd=checked_int(a.get("hwnd"),1,9223372036854775807)
        win=self._window_details(hwnd)
        if not win["visible"]:raise ValueError("Window is hidden")
        rect=win["rect"]
        box=(rect["left"],rect["top"],rect["right"],rect["bottom"])
        if box[2]-box[0]<10 or box[3]-box[1]<10:
            raise ValueError("Window has no drawable area")
        im=ImageGrab.grab(bbox=box,all_screens=True)
        max_width=checked_int(a.get("max_width",1600),320,2400)
        if im.width>max_width:im.thumbnail((max_width,int(im.height*max_width/im.width)))
        memory=io.BytesIO()
        im.save(memory,format="PNG",optimize=True)
        if memory.tell()>4_000_000:
            raise ValueError("Window screenshot exceeds 4 MB; reduce max_width")
        return {"_image":base64.b64encode(memory.getvalue()).decode("ascii"),
                "width":im.width,"height":im.height,"hwnd":hwnd}

    def terminal_send(self,a:dict[str,Any])->dict[str,Any]:
        self.require("gui")
        target=self._interactive_target(a)
        hwnd=target["hwnd"]
        pid=target["pid"]
        text=a.get("text")
        if not isinstance(text,str) or not 1<=len(text)<=2000 or any(ch in text for ch in map(chr,(10,13,0))):
            raise ValueError("Send a single text line of 1-2000 chars")
        submit=a.get("submit",True)
        if not isinstance(submit,bool):
            raise ValueError("submit must be boolean")
        # Never focus a different window: this is an explicit interactive action
        # only on the already-foreground terminal. No effect on user's mouse.
        gui=self._gui()
        if text.isascii():
            for char in text:
                self._target_window(a,foreground=True)
                gui.write(char,interval=0)
        else:
            windows_unicode_type(text,0,focus_guard=lambda:self._target_window(a,foreground=True))
        current=self._target_window(a,foreground=True)
        if current["pid"]!=pid:
            raise RuntimeError("Terminal identity changed: not submitting")
        if submit:
            gui.press("enter")
        return {"hwnd":hwnd,"pid":pid,"typed_characters":len(text),
                "submitted":submit,"foreground_verified":True,"desktop_interference":True}

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
        target=self._interactive_target(a)
        x,y=checked_int(a.get("x"),-32768,32767),checked_int(a.get("y"),-32768,32767)
        rect=target["rect"]
        if not (rect["left"]<=x<rect["right"] and rect["top"]<=y<rect["bottom"]):
            raise AccessDenied("Refusing to click outside the verified target window")
        button=a.get("button","left")
        if button not in ("left","right","middle"):
            raise ValueError("Invalid mouse button")
        clicks=checked_int(a.get("clicks",1),1,2)
        # Desktop injection is never used without explicit opt-in above.
        self._gui().click(x,y,clicks=clicks,button=button)
        return {"clicked":[x,y],"button":button,
                "target_hwnd":target["hwnd"],"desktop_interference":True}

    def type_text(self,a:dict[str,Any])->dict[str,Any]:
        target=self._interactive_target(a)
        text=a.get("text")
        if not isinstance(text,str) or len(text)>4000:
            raise ValueError("Max 4000 characters")
        interval=checked_int(a.get("interval_ms",0),0,200)/1000
        if os.name=="nt":
            windows_unicode_type(text,interval,focus_guard=lambda:self._target_window(a,foreground=True))
        else:
            gui=self._gui()
            for char in text:
                self._target_window(a,foreground=True)
                gui.write(char,interval=interval)
        return {"typed_characters":len(text),"target_hwnd":target["hwnd"],
                "desktop_interference":True}

    def press_keys(self,a:dict[str,Any])->dict[str,Any]:
        target=self._interactive_target(a)
        gui=self._gui()
        keys=a.get("keys")
        if not isinstance(keys,list) or not 1<=len(keys)<=5 or any(
                not isinstance(k,str) or k not in gui.KEYBOARD_KEYS for k in keys):
            raise ValueError("Unknown keyboard key")
        self._target_window(a,foreground=True)
        if len(keys)==1:
            gui.press(keys[0])
        else:
            gui.hotkey(*keys)
        return {"keys":keys,"target_hwnd":target["hwnd"],"desktop_interference":True}

    def scroll_mouse(self,a:dict[str,Any])->dict[str,Any]:
        target=self._interactive_target(a)
        clicks=checked_int(a.get("clicks"),1,30)
        direction=a.get("direction")
        if direction not in ("up","down"):
            raise ValueError("Invalid scroll direction")
        ctypes,wintypes,user32=self._windows()
        cursor=wintypes.POINT()
        if not user32.GetCursorPos(ctypes.byref(cursor)):
            raise RuntimeError("Cannot verify current mouse position")
        rect=target["rect"]
        if not (rect["left"]<=cursor.x<rect["right"] and rect["top"]<=cursor.y<rect["bottom"]):
            raise AccessDenied("Mouse is outside the verified target window; refusing scroll")
        self._gui().scroll(clicks if direction=="up" else -clicks)
        return {"scrolled":clicks,"direction":direction,"target_hwnd":target["hwnd"],
                "desktop_interference":True}

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


def windows_unicode_type(s: str, interval: float, focus_guard: Callable[[], Any] | None = None) -> None:
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
        if not (flags & 0x0002) and focus_guard is not None:
            focus_guard()
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
