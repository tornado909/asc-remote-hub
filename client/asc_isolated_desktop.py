"""ASC GUI Engine: lightweight isolated Win32 desktops (no VM, no cursor injection).

Windows 10/11; Python 3.10+ stdlib, optional Pillow for PrintWindow captures.
This is a desktop automation boundary, NOT a Windows security sandbox.
"""
from __future__ import annotations
import base64
import ctypes
from ctypes import wintypes as wt
from dataclasses import dataclass,field
import io
import os
import re
import secrets
import subprocess
import threading
import time
from typing import Any,Callable

if os.name!="nt":
    raise RuntimeError("ASC Isolated Desktop requires Windows")

P=ctypes.c_void_p
U=ctypes.WinDLL("user32",use_last_error=True)
K=ctypes.WinDLL("kernel32",use_last_error=True)
G=ctypes.WinDLL("gdi32",use_last_error=True)

def bind(lib,name,argtypes,restype):
    fn=getattr(lib,name)
    fn.argtypes=argtypes
    fn.restype=restype
    return fn

create_desktop=bind(U,"CreateDesktopW",[wt.LPCWSTR,wt.LPCWSTR,P,wt.DWORD,wt.DWORD,P],P)
close_desktop=bind(U,"CloseDesktop",[P],wt.BOOL)
thread_desktop=bind(U,"SetThreadDesktop",[P],wt.BOOL)
get_thread_desktop=bind(U,"GetThreadDesktop",[wt.DWORD],P)
get_thread_id=bind(K,"GetCurrentThreadId",[],wt.DWORD)
is_window=bind(U,"IsWindow",[P],wt.BOOL)
enum_desktop=bind(U,"EnumDesktopWindows",[P,P,ctypes.c_ssize_t],wt.BOOL)
enum_children=bind(U,"EnumChildWindows",[P,P,ctypes.c_ssize_t],wt.BOOL)
get_title=bind(U,"GetWindowTextW",[P,wt.LPWSTR,ctypes.c_int],ctypes.c_int)
get_class=bind(U,"GetClassNameW",[P,wt.LPWSTR,ctypes.c_int],ctypes.c_int)
get_pid=bind(U,"GetWindowThreadProcessId",[P,ctypes.POINTER(wt.DWORD)],wt.DWORD)
window_rect=bind(U,"GetWindowRect",[P,ctypes.POINTER(wt.RECT)],wt.BOOL)
get_cursor=bind(U,"GetCursorPos",[ctypes.POINTER(wt.POINT)],wt.BOOL)
get_foreground=bind(U,"GetForegroundWindow",[],P)
get_style=bind(U,"GetWindowLongW",[P,ctypes.c_int],ctypes.c_long)
def _password_edit(hwnd:int)->bool:
    # ES_PASSWORD=0x20. Only check known Edit class, not arbitrary style flags.
    return bool(get_style(P(hwnd),-16)&0x0020)
is_visible=bind(U,"IsWindowVisible",[P],wt.BOOL)
is_enabled=bind(U,"IsWindowEnabled",[P],wt.BOOL)
send_timeout=bind(U,"SendMessageTimeoutW",[P,wt.UINT,ctypes.c_size_t,ctypes.c_ssize_t,wt.UINT,wt.UINT,ctypes.POINTER(ctypes.c_size_t)],P)
print_window=bind(U,"PrintWindow",[P,P,wt.UINT],wt.BOOL)
get_dc=bind(U,"GetDC",[P],P)
release_dc=bind(U,"ReleaseDC",[P,P],ctypes.c_int)
create_dc=bind(G,"CreateCompatibleDC",[P],P)
delete_dc=bind(G,"DeleteDC",[P],wt.BOOL)
create_bitmap=bind(G,"CreateCompatibleBitmap",[P,ctypes.c_int,ctypes.c_int],P)
select_object=bind(G,"SelectObject",[P,P],P)
delete_object=bind(G,"DeleteObject",[P],wt.BOOL)
get_dib=bind(G,"GetDIBits",[P,P,wt.UINT,wt.UINT,P,P,wt.UINT],ctypes.c_int)

class STARTUPINFOW(ctypes.Structure):
    _fields_=[
        ("cb",wt.DWORD),("lpReserved",wt.LPWSTR),("lpDesktop",wt.LPWSTR),
        ("lpTitle",wt.LPWSTR),("dwX",wt.DWORD),("dwY",wt.DWORD),
        ("dwXSize",wt.DWORD),("dwYSize",wt.DWORD),("dwXCountChars",wt.DWORD),
        ("dwYCountChars",wt.DWORD),("dwFillAttribute",wt.DWORD),
        ("dwFlags",wt.DWORD),("wShowWindow",wt.WORD),("cbReserved2",wt.WORD),
        ("lpReserved2",P),("hStdInput",P),("hStdOutput",P),("hStdError",P)]
class PROCESS_INFORMATION(ctypes.Structure):
    _fields_=[("hProcess",P),("hThread",P),("dwProcessId",wt.DWORD),
              ("dwThreadId",wt.DWORD)]
create_process=bind(K,"CreateProcessW",
    [wt.LPCWSTR,wt.LPWSTR,P,P,wt.BOOL,wt.DWORD,P,wt.LPCWSTR,
     ctypes.POINTER(STARTUPINFOW),ctypes.POINTER(PROCESS_INFORMATION)],wt.BOOL)
wait_object=bind(K,"WaitForSingleObject",[P,wt.DWORD],wt.DWORD)
get_exit_code=bind(K,"GetExitCodeProcess",[P,ctypes.POINTER(wt.DWORD)],wt.BOOL)
terminate_process=bind(K,"TerminateProcess",[P,wt.UINT],wt.BOOL)
close_handle=bind(K,"CloseHandle",[P],wt.BOOL)

# Windows Job object: terminate the whole GUI process tree if its owning
# agent crashes or exits. This is a strict lease, not a VM/container.
class JOB_BASIC_LIMIT(ctypes.Structure):
    _fields_=[("PerProcessUserTimeLimit",ctypes.c_int64),
              ("PerJobUserTimeLimit",ctypes.c_int64),
              ("LimitFlags",wt.DWORD),
              ("MinimumWorkingSetSize",ctypes.c_size_t),
              ("MaximumWorkingSetSize",ctypes.c_size_t),
              ("ActiveProcessLimit",wt.DWORD),
              ("Affinity",ctypes.c_size_t),
              ("PriorityClass",wt.DWORD),
              ("SchedulingClass",wt.DWORD)]
class IO_COUNTERS(ctypes.Structure):
    _fields_=[(key,ctypes.c_uint64) for key in (
        "ReadOperationCount","WriteOperationCount","OtherOperationCount",
        "ReadTransferCount","WriteTransferCount","OtherTransferCount")]
class JOB_EXTENDED_LIMIT(ctypes.Structure):
    _fields_=[("BasicLimitInformation",JOB_BASIC_LIMIT),
              ("IoInfo",IO_COUNTERS),
              ("ProcessMemoryLimit",ctypes.c_size_t),
              ("JobMemoryLimit",ctypes.c_size_t),
              ("PeakProcessMemoryUsed",ctypes.c_size_t),
              ("PeakJobMemoryUsed",ctypes.c_size_t)]
create_job=bind(K,"CreateJobObjectW",[P,wt.LPCWSTR],P)
job_settings=bind(K,"SetInformationJobObject",[P,ctypes.c_int,P,wt.DWORD],wt.BOOL)
assign_job=bind(K,"AssignProcessToJobObject",[P,P],wt.BOOL)
resume_thread=bind(K,"ResumeThread",[P],wt.DWORD)
JOB_OBJECT_EXTENDED_LIMIT=9
JOB_KILL_ON_CLOSE=0x00002000
CREATE_SUSPENDED=0x00000004

READ_OBJECTS=0x0001
CREATE_WINDOW=0x0002
ENUMERATE=0x0040
WRITE_OBJECTS=0x0080
# Windows desktop object, not a security boundary: no DESKTOP_SWITCHDESKTOP.
DESKTOP_RIGHTS=READ_OBJECTS|CREATE_WINDOW|ENUMERATE|WRITE_OBJECTS
WM_SETTEXT=0x000C
WM_GETTEXT=0x000D
BM_CLICK=0x00F5
SMTO_ABORTIFHUNG=0x0002
DIB_RGB_COLORS=0
PW_RENDERFULLCONTENT=0x2
WB_LIMIT=80
WIN_CLASS_LIMIT=128
WINDOW_CB=ctypes.WINFUNCTYPE(wt.BOOL,P,ctypes.c_ssize_t)

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_=[("biSize",wt.DWORD),("biWidth",ctypes.c_long),("biHeight",ctypes.c_long),
              ("biPlanes",wt.WORD),("biBitCount",wt.WORD),("biCompression",wt.DWORD),
              ("biSizeImage",wt.DWORD),("biXPelsPerMeter",ctypes.c_long),
              ("biYPelsPerMeter",ctypes.c_long),("biClrUsed",wt.DWORD),
              ("biClrImportant",wt.DWORD)]
class RGBQUAD(ctypes.Structure):
    _fields_=[("rgbBlue",ctypes.c_ubyte),("rgbGreen",ctypes.c_ubyte),
              ("rgbRed",ctypes.c_ubyte),("rgbReserved",ctypes.c_ubyte)]
class BITMAPINFO(ctypes.Structure):
    _fields_=[("bmiHeader",BITMAPINFOHEADER),("bmiColors",RGBQUAD*1)]

def desktop_snapshot():
    point=wt.POINT()
    if not get_cursor(ctypes.byref(point)):
        raise OSError(ctypes.get_last_error(),"GetCursorPos failed")
    return (point.x,point.y,int(get_foreground() or 0))

def last_error(label):
    return OSError(ctypes.get_last_error(),label)

def _text(fn,handle,size):
    dest=ctypes.create_unicode_buffer(size)
    n=fn(P(handle),dest,size)
    return dest.value if n>=0 else ""

def _window_info(hwnd):
    if not is_window(P(hwnd)):
        return None
    pid=wt.DWORD(0)
    get_pid(P(hwnd),ctypes.byref(pid))
    rect=wt.RECT()
    if not window_rect(P(hwnd),ctypes.byref(rect)):
        return None
    return {"hwnd":int(hwnd),"pid":int(pid.value),
            "title":_text(get_title,hwnd,512),
            "class_name":_text(get_class,hwnd,WIN_CLASS_LIMIT),
            "visible":bool(is_visible(P(hwnd))),"enabled":bool(is_enabled(P(hwnd))),
            "rect":{"left":rect.left,"top":rect.top,
                    "right":rect.right,"bottom":rect.bottom}}

def _enumerate(enumerator,handle,limit=100):
    result=[]
    @WINDOW_CB
    def receive(hwnd,_):
        result.append(int(hwnd))
        return len(result)<limit
    # A callback deliberately returning false stops Enum*; return may be 0.
    ctypes.set_last_error(0)
    succeeded=enumerator(P(handle),ctypes.cast(receive,P),0)
    if not succeeded and len(result)<limit:
        # EnumDesktopWindows returns FALSE on an empty desktop in some sessions.
        error=ctypes.get_last_error()
        if error:
            raise OSError(error,"EnumDesktopWindows failed")
    return result

def _worker(hdesk,fn:Callable[[],Any],timeout:float=12)->Any:
    """Attach a fresh Python thread to another Win32 desktop before any UI use."""
    outcome={}
    finished=threading.Event()
    def run():
        original=get_thread_desktop(get_thread_id())
        try:
            if not thread_desktop(P(hdesk)):raise last_error("SetThreadDesktop failed")
            outcome["value"]=fn()
        except BaseException as err:
            outcome["error"]=err
        finally:
            if original:
                # Window-owning threads can only detach after destroying windows.
                outcome["detached"]=bool(thread_desktop(original))
            finished.set()
    thread=threading.Thread(target=run,daemon=True,name="asc-hidden-desktop-op")
    thread.start()
    if not finished.wait(timeout):
        # Windows cannot forcibly terminate stuck Python threads safely.
        raise TimeoutError("Hidden Desktop operation timed out (thread not forcibly killed)")
    if "error" in outcome:raise outcome["error"]
    return outcome.get("value")

class _OwnedProcess:
    """Small native HANDLE owner for a CreateProcessW process on another Desktop."""
    def __init__(self,pid:int,handle:int):
        self.pid=pid
        self.handle=handle
        self.closed=False

    def poll(self):
        if self.closed:return 0
        code=wt.DWORD()
        if not get_exit_code(P(self.handle),ctypes.byref(code)):
            raise last_error("GetExitCodeProcess failed")
        return None if code.value==259 else int(code.value)

    def terminate(self):
        if self.poll() is None and not terminate_process(P(self.handle),1):
            raise last_error("TerminateProcess failed")

    def kill(self):
        self.terminate()

    def wait(self,timeout:float|None=None):
        duration=0xFFFFFFFF if timeout is None else max(0,min(0xFFFFFFFE,int(timeout*1000)))
        result=wait_object(P(self.handle),duration)
        if result==258:raise subprocess.TimeoutExpired(str(self.pid),timeout)
        if result!=0:raise last_error("WaitForSingleObject failed")
        return self.poll()

    def close(self):
        if not self.closed:
            if not close_handle(P(self.handle)):raise last_error("CloseHandle failed")
            self.closed=True


@dataclass
class _Session:
    token:str
    name:str
    desktop:int
    job:int
    created:float=field(default_factory=time.monotonic)
    processes:dict[int,_OwnedProcess]=field(default_factory=dict)
    last_used:float=field(default_factory=time.monotonic)
    closing:bool=False
    interference:bool=False

class IsolatedDesktops:
    """Sessions and their process handles live only within this agent process."""
    def __init__(self,max_desktops:int=3):
        self._sessions={}
        self._lock=threading.RLock()
        self._max=max_desktops

    def _session(self,token):
        if not isinstance(token,str) or not re.fullmatch(r"[a-f0-9]{24}",token):
            raise ValueError("Invalid desktop session ID")
        item=self._sessions.get(token)
        if item is None or item.closing:raise LookupError("Unknown or closing isolated desktop")
        item.last_used=time.monotonic()
        return item

    def create(self)->dict[str,Any]:
        with self._lock:
            if len(self._sessions)>=self._max:
                raise RuntimeError("Maximum 3 isolated desktops per agent reached")
            token=secrets.token_hex(12)
            name="ASC_GUI_"+token
            job=create_job(None,None)
            if not job:raise last_error("CreateJobObjectW failed")
            try:
                policy=JOB_EXTENDED_LIMIT()
                policy.BasicLimitInformation.LimitFlags=JOB_KILL_ON_CLOSE
                if not job_settings(job,JOB_OBJECT_EXTENDED_LIMIT,
                                    ctypes.byref(policy),ctypes.sizeof(policy)):
                    raise last_error("SetInformationJobObject failed")
                handle=create_desktop(name,None,None,0,DESKTOP_RIGHTS,None)
                if not handle:raise last_error("CreateDesktopW failed")
            except BaseException:
                close_handle(job)
                raise
            sess=_Session(token=token,name=name,desktop=int(handle),job=int(job))
            self._sessions[token]=sess
        return {"desktop_id":token,"desktop_name":name,"isolation":"Win32 Desktop, same Windows session",
                "vm":False,"max_desktops":self._max}

    def launch(self,token:str,argv:list[str],cwd:str|None=None)->dict[str,Any]:
        if not isinstance(argv,list) or not 1<=len(argv)<=20 or any(not isinstance(x,str) or not x or "\x00" in x or len(x)>4096 for x in argv):
            raise ValueError("argv must contain 1-20 nonempty strings")
        if not os.path.isabs(argv[0]) or not os.path.isfile(argv[0]):
            raise ValueError("Absolute existing executable path required")
        if cwd is not None and (not os.path.isabs(cwd) or not os.path.isdir(cwd)):
            raise ValueError("cwd must be an existing absolute directory")
        with self._lock:
            sess=self._session(token)
            if len(sess.processes)>=6:raise RuntimeError("Maximum 6 processes per desktop")
            startup=STARTUPINFOW()
            startup.cb=ctypes.sizeof(startup)
            # Python subprocess.STARTUPINFO does not propagate lpDesktop on
            # this CPython build. Use native CreateProcessW deliberately.
            startup.lpDesktop="WinSta0\\"+sess.name
            info=PROCESS_INFORMATION()
            command=ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
            launched=create_process(argv[0],command,None,None,False,
                                    0x00000200|CREATE_SUSPENDED,None,cwd,
                                    ctypes.byref(startup),ctypes.byref(info))
            if not launched:
                raise last_error("CreateProcessW on isolated desktop failed")
            try:
                # Assign BEFORE the first instruction runs: spawned child trees
                # are now automatically killed when the manager handle closes.
                if not assign_job(P(sess.job),info.hProcess):
                    raise last_error("AssignProcessToJobObject failed")
                if resume_thread(info.hThread)==0xFFFFFFFF:
                    raise last_error("ResumeThread failed")
                proc=_OwnedProcess(int(info.dwProcessId),int(info.hProcess))
                sess.processes[proc.pid]=proc
            except BaseException:
                terminate_process(info.hProcess,1)
                close_handle(info.hProcess)
                raise
            finally:
                close_handle(info.hThread)
        return {"desktop_id":token,"pid":proc.pid,"running":proc.poll() is None,
                "program":os.path.basename(argv[0]),"foreground_affected":False}

    def sessions(self)->dict[str,Any]:
        with self._lock:
            return {"sessions":[
                {"desktop_id":entry.token,"desktop_name":entry.name,
                 "processes":sorted(self._owned_pids(entry)),
                 "age_seconds":round(time.monotonic()-entry.created,1)}
                for entry in self._sessions.values() if not entry.closing],
                "limit":self._max}

    def _owned_pids(self,sess):
        return {pid for pid,process in sess.processes.items() if process.poll() is None}

    def windows(self,token:str,with_children:bool=True,limit:int=80):
        if not isinstance(limit,int) or not 1<=limit<=100:raise ValueError("Invalid window limit")
        with self._lock:
            sess=self._session(token)
            owned=self._owned_pids(sess)
            if not owned:return {"desktop_id":token,"windows":[],"active_processes":[]}
            h=sess.desktop
            def inspect():
                top=_enumerate(enum_desktop,h,limit)
                windows=[]
                for root in top:
                    info=_window_info(root)
                    if not info or info["pid"] not in owned:continue
                    info["parent_hwnd"]=None
                    windows.append(info)
                    if with_children and len(windows)<limit:
                        for child in _enumerate(enum_children,root,limit-len(windows)):
                            item=_window_info(child)
                            if item and item["pid"] in owned:
                                item["parent_hwnd"]=root
                                windows.append(item)
                                if len(windows)>=limit:break
                    if len(windows)>=limit:break
                return windows
            found=_worker(h,inspect)
            return {"desktop_id":token,"windows":found[:limit],
                    "active_processes":sorted(owned),"truncated":len(found)>=limit}

    def _validate_window(self,sess,hwnd:int,expected_pid:int,expected_class:str|None=None):
        owned=self._owned_pids(sess)
        if expected_pid not in owned:
            raise PermissionError("Target process is not owned by this isolated desktop")
        info=_window_info(hwnd)
        if not info or info["pid"]!=expected_pid:
            raise LookupError("Window not found or changed ownership")
        if expected_class and info["class_name"]!=expected_class:
            raise LookupError("Window class changed")
        if not info["enabled"] or not info["visible"]:
            raise ValueError("Target control must be enabled and visible on its own Desktop")
        return info

    def action(self,token:str,hwnd:int,expected_pid:int,method:str,text:str|None=None,
               expected_class:str|None=None)->dict[str,Any]:
        if method not in {"set_text","click_button"}:
            raise ValueError("Unsupported background action")
        if not isinstance(hwnd,int) or hwnd<=0 or not isinstance(expected_pid,int) or expected_pid<=0:
            raise ValueError("A valid HWND and PID are required")
        if text is not None and (not isinstance(text,str) or len(text)>4000 or "\0" in text):
            raise ValueError("Invalid text value")
        if method=="set_text" and text is None:
            raise ValueError("Text is required")
        if method!="set_text" and text is not None:
            raise ValueError("Text only applies to set_text")
        with self._lock:
            sess=self._session(token)
            if sess.interference:
                raise PermissionError("This isolated Desktop has been suspended after user focus interference")
            def perform():
                info=self._validate_window(sess,hwnd,expected_pid,expected_class)
                cls=info["class_name"].lower()
                if method=="set_text":
                    if cls=="edit" and _password_edit(hwnd):
                        raise PermissionError("Refusing to change a password input")
                    if cls not in ("edit","windowsforms10.edit.app.0.141b42a_r8_ad1"):
                        # Standard Edit controls only; no arbitrary WM_SETTEXT to unrelated windows.
                        if not cls.startswith("windowsforms10.edit."):
                            raise ValueError("set_text requires a standard Edit control")
                    buffer=ctypes.create_unicode_buffer(text)
                    value=ctypes.c_size_t(0)
                    succeeded=send_timeout(P(hwnd),WM_SETTEXT,0,
                        ctypes.cast(buffer,P).value,SMTO_ABORTIFHUNG,1500,ctypes.byref(value))
                    if not succeeded:raise last_error("WM_SETTEXT did not complete")
                    verify=ctypes.create_unicode_buffer(min(len(text)+32,4097))
                    ok=send_timeout(P(hwnd),WM_GETTEXT,len(verify),
                         ctypes.cast(verify,P).value,SMTO_ABORTIFHUNG,1500,ctypes.byref(value))
                    if not ok and text:
                        raise RuntimeError("WM_GETTEXT verification failed")
                    return {"hwnd":hwnd,"pid":expected_pid,"method":method,
                            "text_verified":verify.value==text,"characters":len(text)}
                if cls!="button" and not cls.startswith("windowsforms10.button."):
                    raise ValueError("click_button requires a standard Win32 Button")
                out=ctypes.c_size_t(0)
                succeeded=send_timeout(P(hwnd),BM_CLICK,0,0,SMTO_ABORTIFHUNG,1500,ctypes.byref(out))
                if not succeeded:raise last_error("BM_CLICK did not complete")
                return {"hwnd":hwnd,"pid":expected_pid,"method":method,
                        "dispatched":True,"effect_verified":False}
            before=desktop_snapshot()
            result=_worker(sess.desktop,perform)
            same=before==desktop_snapshot()
            result["user_desktop_unchanged"]=same
            if not same:
                # Fail closed. The Win32 message might have reached its target;
                # never silently retry after a possible focus-stealing side effect.
                sess.interference=True
                raise RuntimeError("USER_DESKTOP_INTERFERENCE: action may have completed; session suspended until close")
            return result

    def capture(self,token:str,hwnd:int,expected_pid:int,max_width:int=1000):
        try:from PIL import Image
        except ImportError as exc:
            raise RuntimeError("Pillow is required for PrintWindow capture") from exc
        if not isinstance(max_width,int) or not 320<=max_width<=1920:
            raise ValueError("Invalid max_width")
        with self._lock:
            sess=self._session(token)
            def grab():
                info=self._validate_window(sess,hwnd,expected_pid)
                rect=info["rect"]
                width=rect["right"]-rect["left"]
                height=rect["bottom"]-rect["top"]
                if width<=0 or height<=0 or width>2560 or height>1600:
                    raise ValueError("Window has invalid/out-of-bounds dimensions")
                src=get_dc(None)
                if not src:raise last_error("GetDC failed")
                mem=None;bitmap=None;old=None
                try:
                    mem=create_dc(src)
                    if not mem:raise last_error("CreateCompatibleDC failed")
                    bitmap=create_bitmap(src,width,height)
                    if not bitmap:raise last_error("CreateCompatibleBitmap failed")
                    old=select_object(mem,bitmap)
                    rendered=bool(print_window(P(hwnd),mem,PW_RENDERFULLCONTENT))
                    bmi=BITMAPINFO()
                    bmi.bmiHeader.biSize=ctypes.sizeof(BITMAPINFOHEADER)
                    bmi.bmiHeader.biWidth=width
                    bmi.bmiHeader.biHeight=-height
                    bmi.bmiHeader.biPlanes=1
                    bmi.bmiHeader.biBitCount=32
                    bmi.bmiHeader.biCompression=0
                    buf=ctypes.create_string_buffer(width*height*4)
                    lines=get_dib(mem,bitmap,0,height,buf,ctypes.byref(bmi),DIB_RGB_COLORS)
                    if lines!=height:raise last_error("GetDIBits failed")
                    image=Image.frombuffer("RGB",(width,height),buf.raw,"raw","BGRX",0,1)
                    if width>max_width:
                        image=image.resize((max_width,max(1,int(height*max_width/width))))
                    pixels=image.getextrema()
                    blank=all(lo==hi for lo,hi in pixels)
                    io_stream=io.BytesIO()
                    image.save(io_stream,format="PNG")
                    payload=io_stream.getvalue()
                    if len(payload)>2_000_000:
                        raise ValueError("Capture exceeds 2 MB; retry with a smaller max_width")
                    return {"width":image.width,"height":image.height,
                        "printwindow_returned":rendered,"uniform_image":blank,
                        "png_base64":base64.b64encode(payload).decode("ascii"),
                        "png_bytes":len(payload)}
                finally:
                    if old and mem:select_object(mem,old)
                    if bitmap:delete_object(bitmap)
                    if mem:delete_dc(mem)
                    release_dc(None,src)
            before=desktop_snapshot()
            output=_worker(sess.desktop,grab,timeout=15)
            output.update({"desktop_id":token,"hwnd":hwnd,
                           "user_desktop_unchanged":desktop_snapshot()==before})
            return output

    def close(self,token:str)->dict[str,Any]:
        with self._lock:
            sess=self._session(token)
            sess.closing=True
            self._sessions.pop(token)
            stopped=[]
            errors=[]
            # Release the Job Object even if an individual process is wedged.
            for pid,proc in sess.processes.items():
                try:
                    if proc.poll() is None:
                        proc.terminate()
                        try:proc.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            proc.kill();proc.wait(timeout=3)
                    stopped.append(pid)
                except BaseException as exc:
                    errors.append("Process "+str(pid)+": "+type(exc).__name__)
                finally:
                    try:proc.close()
                    except BaseException as exc:errors.append("Handle "+str(pid)+": "+type(exc).__name__)
            # Closing the kill-on-close job terminates all descendants.
            job_ok=bool(close_handle(P(sess.job)))
            ok=bool(close_desktop(P(sess.desktop)))
            return {"desktop_id":token,"processes_stopped":stopped,
                    "job_handle_closed":job_ok,
                    "desktop_handle_closed":ok,
                    "errors":errors,
                    "os_error":ctypes.get_last_error() if not ok else 0}

    def close_all(self):
        for token in list(self._sessions):
            try:self.close(token)
            except Exception:pass
