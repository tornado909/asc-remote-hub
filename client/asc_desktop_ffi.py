"""Optional verified in-process C ABI acceleration for native desktop actions.

The C ABI is used ONLY for strict headless semantic actions, app-scoped
accessibility observations and status. No physical input or screenshots.
Upstream release v1.3.1, ABI major 7, 64-bit Windows.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path
import struct
import threading

ABI_MAJOR = 7
ACTION_BYTES = 96
DLL_SHA256 = "af3e117d4a5ef167a6cb3cc8056d61640acdc20659c855f7cfff6618483ac7e3"
DLL_ARCHIVE_SHA256 = "b2578949ca0b3421b8bd014d1015de4232f69d1ac13fca0026191fd471c5f878"
MAX_OUTPUT = 600_000
ACTION_KIND = {
    "click":0, "set-value":4, "expand":6, "collapse":7,
    "select":8, "toggle":9, "check":10, "uncheck":11,
    "type":17, "clear":18
}
SURFACE_ID = {
    "window":0, "focused":1, "menu":2, "sheet":4, "alert":6,
    "taskbar":8, "system-tray":9, "system-tray-overflow":16,
    "start-menu":17, "action-center":18
}
RC_CODES = {
    -1:"PERM_DENIED",-2:"ELEMENT_NOT_FOUND",-3:"APP_NOT_FOUND",
    -4:"ACTION_FAILED",-5:"ACTION_NOT_SUPPORTED",-6:"STALE_REF",
    -7:"WINDOW_NOT_FOUND",-8:"PLATFORM_NOT_SUPPORTED",-9:"TIMEOUT",
    -10:"INVALID_ARGS",-11:"NOTIFICATION_NOT_FOUND",-12:"INTERNAL",
    -13:"SNAPSHOT_NOT_FOUND",-14:"POLICY_DENIED",
    -15:"AMBIGUOUS_TARGET",-16:"APP_UNRESPONSIVE"
}

def library_path() -> Path:
    if os.name != "nt":
        raise RuntimeError("Windows x64 C API only")
    return Path(__file__).resolve().parents[1] / "bin" / "agent_desktop_ffi.dll"

def available() -> bool:
    if os.name != "nt":
        return False
    try:
        path = library_path()
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 12_000_000:
            return False
        digest = hashlib.sha256()
        with path.open("rb") as f:
            for part in iter(lambda:f.read(65536),b""):
                digest.update(part)
        return digest.hexdigest()==DLL_SHA256
    except (OSError,NotImplementedError):
        return False

class DesktopFFI:
    def __init__(self,state_root:Path)->None:
        if not available():
            raise RuntimeError("FFI_UNAVAILABLE: verified native desktop library missing")
        desired=str(Path(state_root).resolve())
        inherited=os.environ.get("AGENT_DESKTOP_HOME")
        if inherited is not None and os.path.normcase(inherited)!=os.path.normcase(desired):
            raise RuntimeError("FFI_STATE_MISMATCH: cannot override another native user state")
        os.environ["AGENT_DESKTOP_HOME"]=desired
        self.lib=ctypes.CDLL(str(library_path()))
        cptr=ctypes.c_void_p
        self.lib.ad_init.argtypes=[ctypes.c_uint32]
        self.lib.ad_init.restype=ctypes.c_int32
        if self.lib.ad_init(ABI_MAJOR)!=0:
            raise RuntimeError("FFI_ABI_MISMATCH")
        self.lib.ad_action_size.argtypes=[]
        self.lib.ad_action_size.restype=ctypes.c_size_t
        if self.lib.ad_action_size()!=ACTION_BYTES:
            raise RuntimeError("FFI_ACTION_SIZE_MISMATCH")
        self.lib.ad_adapter_create.argtypes=[]
        self.lib.ad_adapter_create.restype=cptr
        self.lib.ad_adapter_create_with_session.argtypes=[ctypes.c_char_p]
        self.lib.ad_adapter_create_with_session.restype=cptr
        self.lib.ad_adapter_destroy.argtypes=[cptr]
        self.lib.ad_free_string.argtypes=[cptr]
        self.lib.ad_status.argtypes=[cptr,ctypes.POINTER(cptr)]
        self.lib.ad_status.restype=ctypes.c_int32
        self.lib.ad_snapshot.argtypes=[cptr,ctypes.c_char_p,ctypes.c_int32,ctypes.c_uint8,
                                        ctypes.c_bool,ctypes.c_bool,ctypes.POINTER(cptr)]
        self.lib.ad_snapshot.restype=ctypes.c_int32
        self.lib.ad_execute_by_ref_timeout.argtypes=[cptr,ctypes.c_char_p,cptr,ctypes.c_int32,
                                                       ctypes.c_int64,ctypes.POINTER(cptr)]
        self.lib.ad_execute_by_ref_timeout.restype=ctypes.c_int32
        self.lock=threading.RLock()
        self.adapters:dict[str,ctypes.c_void_p]={}

    def _adapter(self,sid:str|None):
        from asc_desktop_workflows import session_scope
        if sid is not None:session_scope({"session_id":sid})
        key=sid or ""
        ptr=self.adapters.get(key)
        if ptr:return ptr
        ptr=(self.lib.ad_adapter_create_with_session(sid.encode("ascii"))
             if sid else self.lib.ad_adapter_create())
        if not ptr:raise RuntimeError("FFI_ADAPTER_CREATE_FAILED")
        if len(self.adapters)>=4:
            old=next(iter(self.adapters))
            self.lib.ad_adapter_destroy(self.adapters.pop(old))
        self.adapters[key]=ptr
        return ptr

    @staticmethod
    def _decode(rc:int,pointer:ctypes.c_void_p,library,mutating:bool)->dict:
        from asc_desktop_native import NativeDesktopError
        raw=b""
        try:
            if pointer.value:
                raw=ctypes.string_at(pointer.value)
        finally:
            if pointer.value:
                library.ad_free_string(pointer.value)
        if len(raw)>MAX_OUTPUT:
            raise NativeDesktopError("OUTPUT_TOO_LARGE")
        try:
            result=json.loads(raw.decode("utf-8-sig")) if raw else {}
        except (UnicodeError,ValueError):
            raise NativeDesktopError("ENGINE_INVALID_JSON")
        if rc!=0 or not isinstance(result,dict) or result.get("ok") is not True:
            error=result.get("error",{}) if isinstance(result,dict) else {}
            code=error.get("code") if isinstance(error,dict) else None
            if not isinstance(code,str) or not code.isupper():
                code=RC_CODES.get(rc,"ENGINE_FAILED")
            disposition=error.get("disposition") if isinstance(error,dict) else None
            if not isinstance(disposition,dict):
                disposition={"delivery":"unknown" if mutating else "not_delivered",
                             "retry":"unknown" if mutating else "safe"}
            else:
                disposition={"delivery":disposition.get("delivery","unknown"),
                             "retry":disposition.get("retry","unknown")}
            raise NativeDesktopError(code,disposition)
        return result

    def status(self,sid:str|None=None)->dict:
        with self.lock:
            out=ctypes.c_void_p()
            rc=self.lib.ad_status(self._adapter(sid),ctypes.byref(out))
            return self._decode(rc,out,self.lib,False)

    def snapshot(self,app:str|None,surface:str,depth:int,interactive:bool,compact:bool,
                 sid:str|None=None)->dict:
        if surface not in SURFACE_ID or not 1<=depth<=30:
            raise ValueError("FFI snapshot needs a supported surface and depth")
        with self.lock:
            out=ctypes.c_void_p()
            rc=self.lib.ad_snapshot(self._adapter(sid),app.encode("utf-8") if app else None,
                                    SURFACE_ID[surface],depth,interactive,compact,ctypes.byref(out))
            return self._decode(rc,out,self.lib,False)

    def act(self,reference:str,action:str,value:str|None,timeout:int,
            sid:str|None=None)->dict:
        from asc_desktop_workflows import ref
        target=ref(reference)
        if action not in ACTION_KIND or not 0<=timeout<=25000:
            raise ValueError("Invalid FFI action")
        if action in ("type","set-value","select"):
            if not isinstance(value,str) or not 1<=len(value)<=4000:
                raise ValueError("Action needs text up to 4000 characters")
        elif value is not None:
            raise ValueError("This action does not accept text")
        raw=ctypes.create_string_buffer(ACTION_BYTES)
        struct.pack_into("i",raw,0,ACTION_KIND[action])
        text_buf=None
        if value is not None:
            text_buf=ctypes.create_string_buffer(value.encode("utf-8"))
            ctypes.c_void_p.from_buffer(raw,8).value=ctypes.addressof(text_buf)
        with self.lock:
            out=ctypes.c_void_p()
            rc=self.lib.ad_execute_by_ref_timeout(self._adapter(sid),target.encode("ascii"),
                                                   ctypes.cast(raw,ctypes.c_void_p),0,timeout,
                                                   ctypes.byref(out))
            result=self._decode(rc,out,self.lib,True)
        if value is not None:
            # Native FFI does not implement the CLI --stdin redaction contract.
            # Do not echo user text or postcondition values through MCP.
            def redact(obj):
                if isinstance(obj,dict):
                    return {k:({"redacted":True} if k in ("value","text") else redact(v))
                            for k,v in obj.items()}
                if isinstance(obj,list):return [redact(x) for x in obj]
                return obj
            result=redact(result)
        return result

    def close(self)->None:
        with self.lock:
            while self.adapters:
                _,ptr=self.adapters.popitem()
                self.lib.ad_adapter_destroy(ptr)
