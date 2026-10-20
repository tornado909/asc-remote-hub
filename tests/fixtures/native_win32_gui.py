"""Disposable, deterministic, native Win32 window fixture for ASC GUI Engine."""
import ctypes,json,os,sys,time
from ctypes import wintypes as wt
from pathlib import Path
u=ctypes.WinDLL("user32",use_last_error=True)
k=ctypes.WinDLL("kernel32",use_last_error=True)
P=ctypes.c_void_p
WP=ctypes.c_size_t
LP=ctypes.c_ssize_t
WNDPROC=ctypes.WINFUNCTYPE(LP,P,wt.UINT,WP,LP)
class WNDCLASSW(ctypes.Structure):
    _fields_=[("style",wt.UINT),("lpfnWndProc",WNDPROC),("cbClsExtra",ctypes.c_int),
        ("cbWndExtra",ctypes.c_int),("hInstance",P),("hIcon",P),("hCursor",P),
        ("hbrBackground",P),("lpszMenuName",wt.LPCWSTR),("lpszClassName",wt.LPCWSTR)]
class MSG(ctypes.Structure):
    _fields_=[("hwnd",P),("message",wt.UINT),("wParam",WP),("lParam",LP),
              ("time",wt.DWORD),("pt",wt.POINT)]
def api(name,args,ret):
    f=getattr(u,name);f.argtypes=args;f.restype=ret;return f
register=api("RegisterClassW",[ctypes.POINTER(WNDCLASSW)],wt.WORD)
create=api("CreateWindowExW",[wt.DWORD,wt.LPCWSTR,wt.LPCWSTR,wt.DWORD,
    ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,P,P,P,P],P)
show=api("ShowWindow",[P,ctypes.c_int],wt.BOOL)
update=api("UpdateWindow",[P],wt.BOOL)
get_message=api("GetMessageW",[ctypes.POINTER(MSG),P,wt.UINT,wt.UINT],ctypes.c_int)
translate=api("TranslateMessage",[ctypes.POINTER(MSG)],wt.BOOL)
dispatch=api("DispatchMessageW",[ctypes.POINTER(MSG)],LP)
defproc=api("DefWindowProcW",[P,wt.UINT,WP,LP],LP)
postquit=api("PostQuitMessage",[ctypes.c_int],None)
getwindow=api("GetWindowTextW",[P,wt.LPWSTR,ctypes.c_int],ctypes.c_int)
module=k.GetModuleHandleW;module.argtypes=[wt.LPCWSTR];module.restype=P
prefix=Path(sys.argv[1])
prefix.parent.mkdir(parents=True,exist_ok=True)
state={'clicks':0,'events':[]}
def write():
    (prefix.parent/(prefix.name+'-events.json')).write_text(json.dumps(state),encoding='utf8')
@WNDPROC
def procedure(hwnd,msg,wparam,lparam):
    if msg==0x0111: # WM_COMMAND
        low=int(wparam)&0xffff
        if low==301:
            state['clicks']+=1
            state['events'].append({"kind":"button","n":state['clicks']})
            write()
            return 0
    if msg==0x0002:
        postquit(0);return 0
    return defproc(hwnd,msg,wparam,lparam)
klass='ASC_GUI_ENGINE_NATIVE_FIXTURE'
wc=WNDCLASSW()
wc.lpfnWndProc=procedure
wc.lpszClassName=klass
wc.hInstance=module(None)
wc.hbrBackground=P(6)
atom=register(ctypes.byref(wc))
if not atom:raise OSError(ctypes.get_last_error(),'RegisterClassW')
parent=create(0,klass,'ASC_GUI_ENGINE_TEST_WINDOW',0x00CF0000,
              50,50,480,240,None,None,wc.hInstance,None)
if not parent:raise OSError(ctypes.get_last_error(),'CreateWindowExW main')
edit=create(0x00000200,'EDIT','INITIAL',0x50010080,
            20,30,400,30,parent,P(302),wc.hInstance,None)
if not edit:raise OSError(ctypes.get_last_error(),'CreateWindowExW edit')
button=create(0,'BUTTON','ASC_TRIGGER_BUTTON',0x50010000,
            20,85,250,45,parent,P(301),wc.hInstance,None)
if not button:raise OSError(ctypes.get_last_error(),'CreateWindowExW button')
show(parent,5);update(parent)
thread_id=k.GetCurrentThreadId
thread_id.argtypes=[]
thread_id.restype=wt.DWORD
current_desk=u.GetThreadDesktop
current_desk.argtypes=[wt.DWORD]
current_desk.restype=P
get_user_object=u.GetUserObjectInformationW
get_user_object.argtypes=[P,ctypes.c_int,P,wt.DWORD,ctypes.POINTER(wt.DWORD)]
get_user_object.restype=wt.BOOL
desk_handle=current_desk(thread_id())
buff=ctypes.create_unicode_buffer(128)
length=wt.DWORD()
get_user_object(desk_handle,2,buff,ctypes.sizeof(buff),ctypes.byref(length))
output={'actual_desktop':buff.value,'pid':os.getpid(),'hwnd':int(parent),'edit_hwnd':int(edit),
        'button_hwnd':int(button),'desktop':str(sys.argv[2]) if len(sys.argv)>2 else None}
(prefix.parent/(prefix.name+'-ready.json')).write_text(json.dumps(output),encoding='utf8')
write()
message=MSG()
while True:
    n=get_message(ctypes.byref(message),None,0,0)
    if n<=0:break
    translate(ctypes.byref(message));dispatch(ctypes.byref(message))
