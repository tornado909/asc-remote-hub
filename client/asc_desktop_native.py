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
import time
import threading
from typing import Any
from asc_desktop_workflows import WorkflowMixin,WINDOW_SURFACES,session_scope,native_scope,string,number,boolean,ref

NATIVE_RELEASE = "1.3.1"
# SHA256 of the x86_64 Windows executable extracted from the verified
# official upstream v1.3.1 release archive.
WINDOWS_EXE_SHA256 = "a7ef5e539fb97287a35eb9ed49d3803b4d6bd705bc4106eb28617229f9be679e"
REF_PATTERN = re.compile(r"@s[a-z0-9]+:e[1-9][0-9]{0,8}\Z")
WINDOW_PATTERN = re.compile(r"w-[0-9]+\Z")
MAX_OUTPUT_BYTES = 600_000
MAX_NATIVE_TIMEOUT_MS = 25_000

class NativeDesktopError(RuntimeError):
    """Minimal, machine-readable error; never prints private window content."""
    def __init__(self,code:str,disposition:dict|None=None,recovery:dict|None=None):
        self.code=code
        self.disposition=disposition or {"delivery":"unknown","retry":"unknown"}
        self.recovery=recovery or None
        super().__init__(json.dumps({"code":self.code,"disposition":self.disposition,
                         **({"recovery":self.recovery} if self.recovery else {})},
                         separators=(",",":")))



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


class NativeDesktop(WorkflowMixin):
    def __init__(self, state_dir: Path) -> None:
        if not installed():
            raise NativeDesktopError("Native desktop engine unavailable or SHA256 verification failed")
        self.exe = native_executable()
        self.state = Path(state_dir).resolve() / "native-desktop"
        self._ffi_backend = None
        self._ffi_init_lock = threading.Lock()
        self._deadline_local = threading.local()
        # IMPORTANT: Do not mkdir this on Windows. The native engine must
        # create the state root itself with a private DACL; a Python-created
        # directory may inherit broad ACLs and is rightly rejected.

    @property
    def _batch_deadline(self) -> float | None:
        # Two concurrent sessions must NEVER overwrite one another's timeout.
        return getattr(self._deadline_local,"deadline",None)

    @_batch_deadline.setter
    def _batch_deadline(self,value:float|None)->None:
        self._deadline_local.deadline=value

    def _ffi(self):
        if self._ffi_backend is None:
            with self._ffi_init_lock:
                if self._ffi_backend is None:
                    from asc_desktop_ffi import DesktopFFI
                    self._ffi_backend = DesktopFFI(self.state)
        return self._ffi_backend

    def _run(self,command:str,args:list[str],timeout_ms:int=6000,*,
             session_id:str|None=None,input_text:str|None=None)->dict:
        if not installed():
            raise NativeDesktopError("BINARY_MISMATCH")
        allowed={
            "status","list-windows","list-apps","list-surfaces","session",
            "snapshot","find","get","is","wait","click","set-value","type",
            "clear","toggle","check","uncheck","select","expand","collapse",
            "launch","close-app","focus-window","move-window","resize-window",
            "minimize","maximize","restore",
        }
        if command not in allowed:
            raise ValueError("Unsupported semantic desktop operation")
        forbidden={"--headed","--screenshot","--trace","--debug","--mouse-click","--key","--session"}
        if any(isinstance(arg,str) and arg in forbidden for arg in args):
            raise ValueError("Unsafe native desktop option refused")
        if session_id is not None:
            session_scope({"session_id":session_id})
        if input_text is not None and command not in ("set-value","type"):
            raise ValueError("stdin text is only supported for field edit actions")
        environment=dict(os.environ)
        # Never trust inherited host tracing or cross-request session scope.
        for key in ("AGENT_DESKTOP_SESSION","AGENT_DESKTOP_AGENT_ID",
                    "AGENT_DESKTOP_TRACE","AGENT_DESKTOP_SCREENSHOTS"):
            environment.pop(key,None)
        environment["AGENT_DESKTOP_HOME"]=str(self.state)
        if session_id is not None:
            environment["AGENT_DESKTOP_SESSION"]=session_id
        kwargs={
            "env":environment,"stdout":subprocess.PIPE,"stderr":subprocess.PIPE,
            "timeout":min(80,max(8,timeout_ms/1000+12)),
            "creationflags":getattr(subprocess,"CREATE_NO_WINDOW",0),
            "check":False,
        }
        if input_text is None:
            kwargs["stdin"]=subprocess.DEVNULL
        else:
            kwargs["input"]=input_text.encode("utf-8")
        deadline=getattr(self,"_batch_deadline",None)
        if deadline is not None:
            remaining=deadline-time.monotonic()
            if remaining<=0:
                raise NativeDesktopError("TIMEOUT",{"delivery":"not_delivered","retry":"safe"})
            kwargs["timeout"]=min(kwargs["timeout"],max(0.05,remaining))
        try:
            proc=subprocess.run([str(self.exe),command,*args],**kwargs)
        except subprocess.TimeoutExpired:
            read_only=command in {"status","list-windows","list-apps","list-surfaces",
                "snapshot","find","get","is","wait"}
            raise NativeDesktopError("TIMEOUT",{
                "delivery":"not_delivered" if read_only else "unknown",
                "retry":"safe" if read_only else "unknown"}) from None
        if len(proc.stdout)>MAX_OUTPUT_BYTES:
            raise NativeDesktopError("OUTPUT_TOO_LARGE")
        try:
            result=json.loads(proc.stdout.decode("utf-8-sig"))
        except (UnicodeError,ValueError) as exc:
            raise NativeDesktopError("ENGINE_INVALID_JSON") from exc
        if not isinstance(result,dict) or result.get("ok") is not True or proc.returncode!=0:
            error=result.get("error",{}) if isinstance(result,dict) else {}
            code=error.get("code","ENGINE_FAILED") if isinstance(error,dict) else "ENGINE_FAILED"
            if not isinstance(code,str) or not re.fullmatch(r"[A-Z][A-Z_0-9]{1,48}",code):
                code="ENGINE_FAILED"
            candidate=error.get("disposition") if isinstance(error,dict) else None
            disposition=None
            if isinstance(candidate,dict):
                d=candidate.get("delivery");r=candidate.get("retry")
                if d in ("not_delivered","delivery_uncertain","delivered_unverified",
                         "delivered_verified","unknown") and r in ("safe","unsafe","unknown"):
                    disposition={"delivery":d,"retry":r}
            recovery=None
            safe=error.get("recovery") if isinstance(error,dict) else None
            if isinstance(safe,dict) and disposition and disposition["retry"]=="safe":
                strategy=safe.get("strategy")
                if isinstance(strategy,str) and re.fullmatch(r"[a-z_]{1,40}",strategy):
                    recovery={"strategy":strategy,"requires_fresh_snapshot":
                              safe.get("requires_fresh_snapshot") is True}
            raise NativeDesktopError(code,disposition,recovery)
        if command in ("snapshot","find"):
            self._prune_snapshots(session_id)
        return result

    def _prune_snapshots(self,session_id:str|None=None) -> None:
        """Bound global or session-specific refmaps; never follow symlinks."""
        base=self.state
        if session_id is not None:
            session_scope({"session_id":session_id})
            for piece in ("sessions",session_id):
                if base.is_symlink():
                    return
                base=base/piece
        if base.is_symlink():
            return
        store=base/"snapshots"
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

    def status(self,args:dict)->dict:
        engine=string(args.get("engine"),"engine",8) or "cli"
        if engine=="ffi":
            return self._ffi().status(session_scope(args))
        if engine!="cli":raise ValueError("Engine must be cli or ffi")
        return self._run("status",[],5000,session_id=session_scope(args))

    def windows(self,args:dict)->dict:
        timeout=number(args.get("timeout_ms"),"timeout_ms",8000,500,20000)
        app=string(args.get("app"),"app",128)
        return self._run("list-windows",(["--app",app] if app else [])+
                         ["--timeout-ms",str(timeout)],timeout,session_id=session_scope(args))

    def snapshot(self,args:dict)->dict:
        timeout=number(args.get("timeout_ms"),"timeout_ms",7000,1000,MAX_NATIVE_TIMEOUT_MS)
        depth=number(args.get("max_depth"),"max_depth",8,1,30)
        skeleton=boolean(args.get("skeleton"),"skeleton",False)
        interactive=boolean(args.get("interactive_only"),"interactive_only",True)
        compact=boolean(args.get("compact"),"compact",True)
        scope=native_scope(args,allow_root=True)
        surface=string(args.get("surface"),"surface",28)
        engine=string(args.get("engine"),"engine",8) or "cli"
        if engine=="ffi":
            app=string(args.get("app"),"app",128)
            if args.get("window_id") or args.get("root") or skeleton:
                raise ValueError("FFI snapshot supports app or frontmost window, not window_id/root/skeleton")
            sid=session_scope(args)
            result=self._ffi().snapshot(app,surface or "window",depth,interactive,compact,sid)
            self._prune_snapshots(sid)
            return result
        if engine!="cli":raise ValueError("Engine must be cli or ffi")
        command=[*scope,"--max-depth",str(depth),"--timeout-ms",str(timeout)]
        if surface:
            if surface not in WINDOW_SURFACES:raise ValueError("Unsupported Windows UI surface")
            if args.get("root"):raise ValueError("Surface and root cannot be combined")
            if skeleton:raise ValueError("Surface is already scoped; skeleton is not supported")
            command+=["--surface",surface]
        if interactive:command.append("--interactive-only")
        if compact:command.append("--compact")
        if skeleton:
            if args.get("root"):raise ValueError("skeleton cannot be combined with root")
            command.append("--skeleton")
        return self._run("snapshot",command,timeout,session_id=session_scope(args))

    def find(self,args:dict)->dict:
        timeout=number(args.get("timeout_ms"),"timeout_ms",7000,1000,MAX_NATIVE_TIMEOUT_MS)
        role=string(args.get("role"),"role",64)
        name=string(args.get("name"),"name",180)
        if not role and not name:raise ValueError("role or name is required")
        scope=native_scope(args,allow_root=True)
        surface=string(args.get("surface"),"surface",28)
        command=[*scope,"--timeout-ms",str(timeout)]
        if surface:
            if surface not in WINDOW_SURFACES or args.get("root"):
                raise ValueError("Invalid surface or root combination")
            command+=["--surface",surface]
        if role:command+=["--role",role]
        if name:command+=["--name",name]
        if boolean(args.get("exact"),"exact",True):command.append("--exact")
        if boolean(args.get("first"),"first",False):command.append("--first")
        return self._run("find",command,timeout,session_id=session_scope(args))

    def get(self,args:dict)->dict:
        prop=string(args.get("property"),"property",20) or "text"
        if prop not in ("text","value","title","bounds","role","states"):
            raise ValueError("Invalid property")
        return self._run("get",[ref(args.get("ref")),"--property",prop],
                         7000,session_id=session_scope(args))

    def is_state(self,args:dict)->dict:
        prop=string(args.get("property"),"property",20,True)
        if prop not in ("visible","enabled","checked","focused","expanded","selected"):
            raise ValueError("Invalid state property")
        return self._run("is",[ref(args.get("ref")),"--property",prop],
                         7000,session_id=session_scope(args))

    def act(self,args:dict)->dict:
        action=string(args.get("action"),"action",16,True)
        if action not in ("click","set-value","type","clear","toggle","check",
                          "uncheck","select","expand","collapse"):
            raise ValueError("Only semantic accessibility actions are supported")
        target=ref(args.get("ref"))
        timeout=number(args.get("timeout_ms"),"timeout_ms",7000,1000,MAX_NATIVE_TIMEOUT_MS)
        value=args.get("value")
        stdin_text=None
        command=[target]
        if action in ("set-value","type"):
            stdin_text=string(value,"value",4000,True)
            command.append("--stdin")
        elif action=="select":
            # Upstream CLI select requires a positional VALUE and does not
            # support --stdin. Never expose that user-provided text in argv.
            value=string(value,"value",256,True)
        elif value is not None:
            raise ValueError("Only set-value, type or select accept text")
        command+=["--timeout-ms",str(timeout)]
        ready=string(args.get("wait_for"),"wait_for",180)
        gone=string(args.get("wait_for_gone"),"wait_for_gone",180)
        if ready and gone:raise ValueError("Choose wait_for or wait_for_gone")
        wait_timeout=None
        if ready or gone:
            from asc_desktop_workflows import ACTION_WAIT
            expected=ready or gone
            if not ACTION_WAIT.fullmatch(expected):
                raise ValueError("Invalid role:text action wait selector")
            wait_timeout=number(args.get("wait_timeout_ms"),"wait_timeout_ms",10000,200,30000)
            command+=["--wait-for" if ready else "--wait-for-gone",expected,
                      "--wait-timeout",str(wait_timeout)]
        elif args.get("wait_timeout_ms") is not None:
            raise ValueError("wait_timeout_ms requires wait_for or wait_for_gone")
        engine=string(args.get("engine"),"engine",8) or "cli"
        if engine=="ffi":
            if ready or gone:
                raise ValueError("FFI actions do not support built-in post-action waits; use desktop_wait")
            deadline=self._batch_deadline
            if deadline is not None:
                remaining=round((deadline-time.monotonic())*1000)
                if remaining<=0:
                    raise NativeDesktopError("TIMEOUT",{"delivery":"not_delivered","retry":"safe"})
                timeout=min(timeout,remaining)
            return self._ffi().act(target,action,value,timeout,session_scope(args))
        if engine!="cli":raise ValueError("Engine must be cli or ffi")
        if action=="select":
            if ready or gone:
                raise ValueError("Use desktop_wait after selection; no CLI text arguments are permitted")
            # Select via verified in-process library even for default CLI mode:
            # the original binary has NO --stdin option for select.
            from asc_desktop_ffi import available as ffi_available
            if not ffi_available():
                raise NativeDesktopError(
                    "SELECT_REQUIRES_FFI",
                    {"delivery":"not_delivered","retry":"safe"})
            deadline=self._batch_deadline
            if deadline is not None:
                remaining=round((deadline-time.monotonic())*1000)
                if remaining<=0:
                    raise NativeDesktopError("TIMEOUT",{"delivery":"not_delivered","retry":"safe"})
                timeout=min(timeout,remaining)
            return self._ffi().act(target,"select",value,timeout,session_scope(args))
        result=self._run(action,command,timeout+(wait_timeout or 0),
                         session_id=session_scope(args),input_text=stdin_text)
        if action in ("set-value","type"):
            # Even the *result* may contain a field's contents. Report only
            # native, typed delivery status to avoid echoing sensitive text.
            data=result.get("data") if isinstance(result,dict) else None
            clean={}
            if isinstance(data,dict):
                disposition=data.get("disposition")
                if isinstance(disposition,dict):
                    clean["disposition"]={k:disposition[k] for k in ("delivery","retry")
                                          if k in disposition and isinstance(disposition[k],str)}
                for key in ("action_effect_verified","cursor_unchanged",
                            "foreground_unchanged","requires_new_snapshot"):
                    if isinstance(data.get(key),bool):
                        clean[key]=data[key]
            return {"ok":True,"data":clean,"input_redacted":True}
        return result
