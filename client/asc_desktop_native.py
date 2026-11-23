"""Optional native semantic desktop bridge. No screenshots, Node.js or shell.

Uses an independently installed, SHA-256-pinned agent-desktop executable.
Its refs, actionability checks and stale-element resolution remain owned by
the upstream native engine. The Hub only supplies bounded, headless commands.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

NATIVE_RELEASE = "1.3.1"
# SHA256 of the x86_64 Windows executable extracted from the verified
# official upstream v1.3.1 release archive.
WINDOWS_EXE_SHA256 = "a7ef5e539fb97287a35eb9ed49d3803b4d6bd705bc4106eb28617229f9be679e"
REF_PATTERN = re.compile(r"@s[a-z0-9]+:e[1-9][0-9]{0,8}\Z")
WINDOW_PATTERN = re.compile(r"w-[0-9]+\Z")
MAX_OUTPUT_BYTES = 600_000
MAX_NATIVE_TIMEOUT_MS = 25_000

class NativeDesktopError(RuntimeError):
    pass


def native_executable() -> Path:
    if os.name != "nt":
        raise NativeDesktopError("Native desktop integration is enabled on Windows only")
    return Path(__file__).resolve().parents[1] / "bin" / "agent-desktop.exe"


def installed() -> bool:
    """Never advertise a locally replaced or unverified executable."""
    if os.name != "nt":
        return False
    try:
        exe = native_executable()
        if not exe.is_file() or exe.is_symlink() or exe.stat().st_size > 12_000_000:
            return False
        h = hashlib.sha256()
        with exe.open("rb") as src:
            for block in iter(lambda: src.read(65536), b""):
                h.update(block)
        return h.hexdigest() == WINDOWS_EXE_SHA256
    except (OSError, NotImplementedError):
        return False


def _bounded_text(value: Any, name: str, limit: int, required: bool=False) -> str:
    if value is None:
        if required:
            raise ValueError(f"{name} is required")
        return ""
    if not isinstance(value,str) or "\x00" in value or len(value)>limit:
        raise ValueError(f"Invalid {name}")
    if required and not value:
        raise ValueError(f"{name} is required")
    return value


def _bounded_int(value: Any, name: str, default: int, low: int, high: int) -> int:
    if value is None:
        return default
    if isinstance(value,bool) or not isinstance(value,int) or value<low or value>high:
        raise ValueError(f"{name} must be an integer between {low} and {high}")
    return value


def _boolean(value: Any, name: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value,bool):
        raise ValueError(f"{name} must be boolean")
    return value


def _ref(value: Any) -> str:
    s=_bounded_text(value,"ref",80,True)
    if not REF_PATTERN.fullmatch(s):
        raise ValueError("Expected a qualified @snapshot:eN ref from the desktop engine")
    return s


class NativeDesktop:
    def __init__(self, state_dir: Path) -> None:
        if not installed():
            raise NativeDesktopError("Native desktop engine unavailable or SHA256 verification failed")
        self.exe = native_executable()
        self.state = Path(state_dir).resolve() / "native-desktop"
        # IMPORTANT: Do not mkdir this on Windows. The native engine must
        # create the state root itself with a private DACL; a Python-created
        # directory may inherit broad ACLs and is rightly rejected.

    def _run(self, command: str, args: list[str], timeout_ms: int=6000) -> dict:
        if not installed():
            raise NativeDesktopError("Native desktop engine changed; refusing execution")
        if command not in {
            "status","list-windows","snapshot","find","get","is","click","set-value",
            "type","clear","toggle","check","uncheck","select","expand","collapse"
        }:
            raise ValueError("Unsupported semantic desktop operation")
        environment={**os.environ, "AGENT_DESKTOP_HOME":str(self.state)}
        # The native CLI defaults to strict headless semantic delivery.
        # Never permit --headed, physical clicks, screenshots, arbitrary shell
        # arguments, tracing, subprocess startup, or interactive stdin here.
        if any(flag in args for flag in (
                "--headed","--screenshot","--trace","--debug",
                "--mouse-click","--key","--session")):
            raise ValueError("Unsafe native desktop option refused")
        try:
            proc=subprocess.run(
                [str(self.exe),command,*args],
                env=environment,stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                timeout=min(42,max(8,timeout_ms/1000+10)),
                creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0),
                check=False
            )
        except subprocess.TimeoutExpired as exc:
            raise NativeDesktopError("TIMEOUT: semantic desktop operation exceeded its deadline") from None
        if len(proc.stdout)>MAX_OUTPUT_BYTES:
            raise NativeDesktopError("OUTPUT_TOO_LARGE: narrow the requested accessibility tree")
        try:
            result=json.loads(proc.stdout.decode("utf-8-sig"))
        except (UnicodeError,ValueError) as exc:
            raise NativeDesktopError("ENGINE_INVALID_JSON: semantic desktop did not return JSON") from exc
        if not isinstance(result,dict) or result.get("ok") is not True or proc.returncode!=0:
            error=result.get("error",{}) if isinstance(result,dict) else {}
            code=error.get("code","ENGINE_FAILED") if isinstance(error,dict) else "ENGINE_FAILED"
            if not isinstance(code,str) or not re.fullmatch(r"[A-Z][A-Z_0-9]{1,48}",code):
                code="ENGINE_FAILED"
            # Return machine-readable error category, not untrusted UI text or
            # full process stderr. No sensitive desktop data enter logs.
            raise NativeDesktopError(code)
        if command in ("snapshot","find"):
            self._prune_snapshots()
        return result

    def _prune_snapshots(self) -> None:
        """Bound local refmap growth; never follow symlinks or touch other apps."""
        store=self.state/"snapshots"
        if not store.is_dir() or store.is_symlink():
            return
        try:
            dirs=[
                item for item in store.iterdir()
                if item.is_dir() and not item.is_symlink()
                and re.fullmatch(r"s[a-z0-9]+",item.name)
            ]
            # Keep last 32 snapshots, including the newest returned by CLI.
            if len(dirs)<=32:
                return
            dirs.sort(key=lambda item:item.stat().st_mtime,reverse=True)
            for item in dirs[32:]:
                shutil.rmtree(item)
        except OSError:
            # Cache housekeeping must never make a successful UI operation fail.
            pass

    @staticmethod
    def _scope(arguments:dict, *, allow_root:bool=False) -> list[str]:
        window_id=_bounded_text(arguments.get("window_id"),"window_id",32)
        app=_bounded_text(arguments.get("app"),"app",128)
        root=_bounded_text(arguments.get("root"),"root",80) if allow_root else ""
        if window_id and not WINDOW_PATTERN.fullmatch(window_id):
            raise ValueError("Invalid window_id: use list-windows")
        if root and not REF_PATTERN.fullmatch(root):
            raise ValueError("Invalid root ref")
        if sum(bool(item) for item in (window_id,app,root))>1:
            raise ValueError("Choose only one window_id, app, or root")
        if root:
            return ["--root",root]
        if window_id:
            return ["--window-id",window_id]
        if app:
            return ["--app",app]
        return []

    def status(self, args:dict) -> dict:
        return self._run("status",[],5000)

    def windows(self, args:dict) -> dict:
        timeout=_bounded_int(args.get("timeout_ms"),"timeout_ms",8000,500,20_000)
        app=_bounded_text(args.get("app"),"app",128)
        return self._run("list-windows",(["--app",app] if app else [])+
                          ["--timeout-ms",str(timeout)],timeout)

    def snapshot(self, args:dict) -> dict:
        timeout=_bounded_int(args.get("timeout_ms"),"timeout_ms",7000,1000,MAX_NATIVE_TIMEOUT_MS)
        depth=_bounded_int(args.get("max_depth"),"max_depth",8,1,30)
        skeleton=_boolean(args.get("skeleton"),"skeleton",False)
        interactive=_boolean(args.get("interactive_only"),"interactive_only",True)
        compact=_boolean(args.get("compact"),"compact",True)
        scope=self._scope(args,allow_root=True)
        command=[*scope,"--max-depth",str(depth),"--timeout-ms",str(timeout)]
        if interactive:command.append("--interactive-only")
        if compact:command.append("--compact")
        if skeleton:
            if "--root" in command:
                raise ValueError("skeleton cannot be combined with root")
            command.append("--skeleton")
        return self._run("snapshot",command,timeout)

    def find(self, args:dict) -> dict:
        timeout=_bounded_int(args.get("timeout_ms"),"timeout_ms",7000,1000,MAX_NATIVE_TIMEOUT_MS)
        role=_bounded_text(args.get("role"),"role",64)
        name=_bounded_text(args.get("name"),"name",180)
        if not role and not name:
            raise ValueError("role or name is required")
        scope=self._scope(args,allow_root=True)
        command=[*scope,"--timeout-ms",str(timeout)]
        if role:command+=["--role",role]
        if name:command+=["--name",name]
        if _boolean(args.get("exact"),"exact",True):command.append("--exact")
        if _boolean(args.get("first"),"first",False):command.append("--first")
        return self._run("find",command,timeout)

    def get(self, args:dict) -> dict:
        prop=_bounded_text(args.get("property"),"property",20) or "text"
        if prop not in ("text","value","title","bounds","role","states"):
            raise ValueError("Invalid property")
        return self._run("get",[_ref(args.get("ref")),"--property",prop],7000)

    def is_state(self, args:dict) -> dict:
        prop=_bounded_text(args.get("property"),"property",20,True)
        if prop not in ("visible","enabled","checked","focused","expanded","selected"):
            raise ValueError("Invalid state property")
        return self._run("is",[_ref(args.get("ref")),"--property",prop],7000)

    def act(self, args:dict) -> dict:
        action=_bounded_text(args.get("action"),"action",16,True)
        if action not in ("click","set-value","type","clear","toggle",
                          "check","uncheck","select","expand","collapse"):
            raise ValueError("Only semantic accessibility actions are supported")
        ref=_ref(args.get("ref"))
        timeout=_bounded_int(args.get("timeout_ms"),"timeout_ms",7000,1000,MAX_NATIVE_TIMEOUT_MS)
        value=args.get("value")
        if action in ("set-value","type","select"):
            value=_bounded_text(value,"value",4000,True)
        elif value is not None:
            raise ValueError("Only set-value, type, or select accept value")
        command=[ref]
        if action in ("set-value","type","select"):command.append(value)
        command+=["--timeout-ms",str(timeout)]
        return self._run(action,command,timeout)
