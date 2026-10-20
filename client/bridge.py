#!/usr/bin/env python3
"""ASC Remote Hub device agent. Outbound HTTPS ONLY. Python 3.10+, stdlib.

Private device credential is encrypted with Windows DPAPI or stored with 0600
permissions on Linux. No OpenAI API keys and no Desktop Commander required.
"""
from __future__ import annotations
import argparse
import base64
import ctypes
from ctypes import wintypes
import tempfile
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import threading
import getpass
import json
import os
from pathlib import Path
import platform
import ssl
import subprocess
import sys
import time
from urllib.error import HTTPError,URLError
from urllib.request import Request,urlopen
from urllib.parse import urlparse
from asc_remote_commander import Agent

VERSION="1.1.3"


def default_config() -> Path:
    if os.name=="nt":
        return Path(os.environ.get("APPDATA",str(Path.home())))/"ASC-Remote-Hub"/"agent.json"
    return Path.home()/".config"/"asc-remote-hub"/"agent.json"


def check_url(url: str):
    p=urlparse(url)
    if p.scheme!="https" and os.environ.get("ASC_TEST_MODE")!="1":
        raise ValueError("Hub URL must be HTTPS (TLS verification always enabled)")
    if not p.netloc or p.path not in ("","/") or p.query or p.fragment or p.username:
        raise ValueError("Hub URL must be an origin without path/query/userinfo")


def request(hub: str, path: str, data: dict, token: str="", device_id: str="", timeout: float=30) -> dict:
    raw=json.dumps(data,ensure_ascii=False,separators=(",",":")).encode("utf-8")
    headers={"Content-Type":"application/json","User-Agent":"ASC-Remote-Hub-Agent/"+VERSION}
    if token:
        headers["Authorization"]="Bearer "+token
        headers["X-ASC-Device"]=device_id
    req=Request(hub.rstrip("/")+path,raw,headers,method="POST")
    with urlopen(req,timeout=timeout,context=ssl.create_default_context()) as resp:
        result=json.loads(resp.read(10*1024*1024))
    if not isinstance(result,dict):
        raise ValueError("Invalid hub response")
    return result


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _windows_dpapi(data: bytes, *, decrypt: bool = False) -> bytes:
    """Windows CryptoAPI DPAPI bound to the interactive Windows user.

    Invoke the OS API directly; never pass device tokens to PowerShell or the
    command line. CryptProtectData requires a loaded user profile.
    """
    if os.name != "nt":
        raise RuntimeError("Windows DPAPI requires Windows")
    crypto = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    crypto.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob), ctypes.c_wchar_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob),
    ]
    crypto.CryptProtectData.restype = wintypes.BOOL
    crypto.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob),
    ]
    crypto.CryptUnprotectData.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    source_buffer = ctypes.create_string_buffer(data)
    source = _DataBlob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    destination = _DataBlob()
    operation = crypto.CryptUnprotectData if decrypt else crypto.CryptProtectData
    if not operation(ctypes.byref(source), None, None, None, None, 0,
                     ctypes.byref(destination)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(destination.pbData, destination.cbData)
    finally:
        kernel.LocalFree(ctypes.cast(destination.pbData, ctypes.c_void_p))


def encrypt_secret(token: str) -> str:
    return "dpapi1:" + base64.b64encode(_windows_dpapi(token.encode("utf-8"))).decode("ascii")


def decrypt_secret(value: str) -> str:
    if value.startswith("dpapi1:"):
        data = base64.b64decode(value[len("dpapi1:"):], validate=True)
        return _windows_dpapi(data, decrypt=True).decode("utf-8")
    # Backwards compatibility for already-paired Windows devices (v1.0.0).
    return powershell_decrypt(value)


def preflight_credentials(config_path: Path):
    """Fail BEFORE redeeming a single-use pairing code."""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        probe = "ASC-REMOTE-HUB-WINDOWS-DPAPI-SELFTEST"
        if decrypt_secret(encrypt_secret(probe)) != probe:
            raise RuntimeError("Windows DPAPI self-test did not roundtrip")
    fd, filename = tempfile.mkstemp(prefix=".asc-key-check-", dir=config_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write("ok")
    finally:
        Path(filename).unlink(missing_ok=True)


def powershell_decrypt(value: str) -> str:
    script=("$ErrorActionPreference='Stop';"
            "$cipher=[Console]::In.ReadToEnd().Trim();"
            "$sec=ConvertTo-SecureString $cipher;"
            "$ptr=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec);"
            "try {[Console]::Out.Write([Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr))}"
            "finally{[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)}")
    result=subprocess.run(["powershell.exe","-NoProfile","-NonInteractive","-Command",script],
                          input=value.encode("utf-8"),capture_output=True,check=True,timeout=20)
    return result.stdout.decode("utf-8").strip()


def secure_write(path: Path, contents: str):
    path.parent.mkdir(parents=True,exist_ok=True)
    if os.name!="nt":
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        with os.fdopen(fd,"w",encoding="utf-8") as output:
            output.write(contents)
        os.chmod(path,0o600)
    else:
        path.write_text(contents,encoding="utf-8")


def enroll(args):
    hub=args.hub.rstrip("/")
    check_url(hub)
    if not args.roots or any(not Path(root).resolve().is_dir() for root in args.roots):
        raise ValueError("Every allowed root must be an existing directory")
    permissions={"write":bool(args.write),"exec":bool(args.exec),
                 "gui":bool(args.gui),"delete":bool(args.delete)}
    os.environ["ASC_RC_ROOTS"]=";".join(args.roots)
    for prop,env in (("write","ASC_RC_ALLOW_WRITE"),("exec","ASC_RC_ALLOW_EXEC"),
                     ("gui","ASC_RC_ALLOW_GUI"),("delete","ASC_RC_ALLOW_DELETE")):
        os.environ[env]="1" if permissions[prop] else "0"
    agent=Agent()
    confpath=Path(args.config).expanduser().resolve()
    secretpath=confpath.with_name("device-key.dpapi" if os.name=="nt" else "device-key.secret")
    if confpath.exists():
        raise RuntimeError("This device is already configured. Reuse its existing pairing.")
    preflight_credentials(confpath)
    code=os.environ.pop("ASC_HUB_PAIRING_CODE","") or getpass.getpass("One-time pairing code from your ASC Hub: ")
    result=request(hub,"/agent/enroll",{"pairing_code":code,"name":args.name,
                                         "platform":platform.platform(),"tools":list(agent.tools)})
    token=result.get("device_token","")
    if not isinstance(token,str) or len(token)<32:
        raise ValueError("Hub did not return a valid device token")
    secure_write(secretpath,encrypt_secret(token) if os.name=="nt" else token)
    secure_write(confpath,json.dumps({"hub_url":hub,"device_id":result["device_id"],
                                       "token_file":str(secretpath),"name":args.name,
                                       "roots":args.roots,"permissions":permissions},
                                      ensure_ascii=False,indent=2))
    print("Device paired successfully; ID:",result["device_id"])
    print("Config:",confpath)
    print("API tokens were never printed or saved in plaintext on Windows.")


def load_config(path: Path):
    # Accept UTF-8 with or without a BOM (e.g. Windows PowerShell 5.1 output).
    conf=json.loads(path.read_text(encoding="utf-8-sig"))
    check_url(conf["hub_url"])
    tokenpath=Path(conf["token_file"])
    encrypted=tokenpath.read_text(encoding="utf-8").strip()
    token=decrypt_secret(encrypted) if os.name=="nt" else encrypted
    if not isinstance(token,str) or len(token)<32:
        raise ValueError("Invalid locally stored device credential")
    roots=conf.get("roots",[])
    if not isinstance(roots,list) or not roots:
        raise ValueError("No allowed paths configured")
    os.environ["ASC_RC_ROOTS"]=";".join(roots)
    permissions=conf.get("permissions",{})
    for prop,env in (("write","ASC_RC_ALLOW_WRITE"),("exec","ASC_RC_ALLOW_EXEC"),
                     ("gui","ASC_RC_ALLOW_GUI"),("delete","ASC_RC_ALLOW_DELETE")):
        os.environ[env]="1" if permissions.get(prop,False) else "0"
    os.environ["ASC_RC_STATE_DIR"]=str(path.parent/"audit")
    return conf,token,Agent()


HEARTBEAT_INTERVAL_SECONDS = 12


@contextmanager
def exclusive_agent_lock(config_path: Path):
    """Prevent two agents with the same device token from polling concurrently.

    Kernel locks are released automatically after abrupt process termination.
    Linux flock and Windows msvcrt byte locks work without third-party packages.
    """
    lockfile = config_path.with_name("agent.lock")
    lockfile.parent.mkdir(parents=True,exist_ok=True)
    fd = os.open(lockfile,os.O_CREAT|os.O_RDWR,0o600)
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt
            if os.fstat(fd).st_size == 0:
                os.write(fd,b"0")
            os.lseek(fd,0,os.SEEK_SET)
            try:
                msvcrt.locking(fd,msvcrt.LK_NBLCK,1)
            except OSError as exc:
                raise RuntimeError("ASC Remote Hub agent already running for this account") from exc
        else:
            import fcntl
            try:
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("ASC Remote Hub agent already running for this account") from exc
        acquired = True
        yield
    finally:
        if acquired:
            if os.name=="nt":
                os.lseek(fd,0,os.SEEK_SET)
                msvcrt.locking(fd,msvcrt.LK_UNLCK,1)
            else:
                fcntl.flock(fd,fcntl.LOCK_UN)
        os.close(fd)


def busy_heartbeat(busy: threading.Event, stop: threading.Event,
                   revoked: threading.Event, send_hello, interval=HEARTBEAT_INTERVAL_SECONDS):
    """Keep the device online while a potentially slow tool call is executing."""
    while not stop.wait(interval):
        if not busy.is_set():
            continue
        try:
            send_hello()
        except HTTPError as exc:
            if exc.code==401:
                revoked.set()
                print("Agent heartbeat rejected authentication (401)",file=sys.stderr,flush=True)
                return
            print("Agent heartbeat HTTP",exc.code,file=sys.stderr,flush=True)
        except (URLError,TimeoutError,OSError,ValueError) as exc:
            print("Agent heartbeat unavailable:",type(exc).__name__,file=sys.stderr,flush=True)


def record_connected_marker(device_id: str, *, directory: Path | None = None) -> bool:
    """Mark a server-acknowledged hello for the Windows installer.

    The marker contains only the device ID, process PID, version and time.
    It never contains the device credential or any API response.
    """
    if directory is None:
        if os.name != "nt":
            return False
        directory = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ASC-Remote-Hub" / "logs"
    temporary = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".connected-", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump({"device_id": device_id, "connected_at": time.time(),
                       "pid": os.getpid(), "version": VERSION}, out)
        os.replace(temporary, directory / "agent.ready.json")
        return True
    except OSError as exc:
        print("Could not save agent readiness marker:", type(exc).__name__,
              file=sys.stderr, flush=True)
        return False
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)


# Only explicit non-mutating reads can bypass the side-effect lane.
READ_ONLY_JOBS = frozenset({
    "machine_info","list_directory","stat_path","stat_paths","read_file",
    "read_files","directory_tree","search_text","search_files","read_process",
    "window_list"
})
GUI_JOBS = frozenset({
    "screenshot","window_screenshot","window_focus","window_controls","window_action","terminal_send",
    "click_mouse","type_text","press_keys","scroll_mouse"
})
MAX_CONCURRENT_JOBS = 4


def execute_agent_job(agent, tool: str, args: dict,
                      mutation_lock: threading.RLock,
                      gui_lock: threading.RLock,
                      deadline: float | None = None):
    """Run independent reads concurrently; never start a side-effect after
    the server-side job lease has expired while waiting on a lock.
    """
    def expired():
        return deadline is not None and time.monotonic()>=deadline
    def rejection():
        return {"content":[{"type":"text",
                             "text":"ExpiredBeforeExecution: task was not started"}],
                "isError":True}
    if expired():return rejection()
    lock=gui_lock if tool in GUI_JOBS else (
         None if tool in READ_ONLY_JOBS else mutation_lock)
    if lock is None:
        return agent.handle_tool(tool,args)
    if deadline is not None:
        obtained=lock.acquire(timeout=max(0,deadline-time.monotonic()))
        if not obtained:return rejection()
    else:
        lock.acquire()
    try:
        if expired():return rejection()
        return agent.handle_tool(tool,args)
    finally:
        lock.release()


def run(args):
    config_path=Path(args.config).expanduser().resolve()
    with exclusive_agent_lock(config_path):
        conf,token,agent=load_config(config_path)
        hub,did=conf["hub_url"],conf["device_id"]
        print("ASC Remote Hub agent started:",did,"version",VERSION,flush=True)
        busy=threading.Event()
        stopped=threading.Event()
        revoked=threading.Event()
        mutation_lock=threading.RLock()
        gui_lock=threading.RLock()
        hello={"machine_info":agent.machine_info({}),
               "tools":list(agent.tools),"version":VERSION}
        def send_hello():
            return request(hub,"/agent/hello",hello,token,did,timeout=9)
        heart=threading.Thread(
            target=busy_heartbeat,args=(busy,stopped,revoked,send_hello),
            daemon=True,name="asc-hub-heartbeat")
        heart.start()

        def process_job(job):
            job_id=job.get("id")
            tool=job.get("tool","")
            args=job.get("arguments") or {}
            ttl=job.get("ttl_seconds")
            # Reserve two seconds for relaying the result before the server timeout.
            deadline=(time.monotonic()+max(0,float(ttl)-2)
                      if isinstance(ttl,(int,float)) and not isinstance(ttl,bool)
                      else None)
            result=execute_agent_job(agent,tool,args,mutation_lock,gui_lock,deadline)
            for attempt in range(3):
                try:
                    request(hub,"/agent/result",{"id":job_id,"result":result},
                            token,did,timeout=35)
                    return
                except HTTPError as exc:
                    # Client/network retries never re-run the side effect.
                    if exc.code==401:
                        revoked.set()
                    if exc.code < 500:
                        print("Result rejected:",exc.code,file=sys.stderr,flush=True)
                        return
                    print("Result HTTP retry:",exc.code,file=sys.stderr,flush=True)
                except (URLError,TimeoutError,OSError) as exc:
                    print("Result transport retry:",type(exc).__name__,
                          file=sys.stderr,flush=True)
                if attempt<2:
                    time.sleep(0.5*(attempt+1))
            print("Could not deliver job result:",job_id,
                  file=sys.stderr,flush=True)

        delay=2
        try:
            with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_JOBS,
                                    thread_name_prefix="asc-hub-worker") as pool:
                futures=set()
                while not revoked.is_set():
                    try:
                        hello_reply=send_hello()
                        if (hello_reply.get("ok") is not True or
                                hello_reply.get("device_id") != did):
                            raise ValueError("Hub did not acknowledge this device's hello")
                        record_connected_marker(did)
                        delay=2
                        while not revoked.is_set():
                            if len(futures)>=MAX_CONCURRENT_JOBS:
                                finished,_=wait(futures,timeout=1,
                                                 return_when=FIRST_COMPLETED)
                                futures.difference_update(finished)
                                for future in finished:
                                    try:future.result()
                                    except Exception as exc:
                                        print("Worker job error:",type(exc).__name__,
                                              file=sys.stderr,flush=True)
                                if not futures:busy.clear()
                                continue
                            item=request(hub,"/agent/next",{},token,did,timeout=29)
                            job=item.get("job")
                            if isinstance(job,dict):
                                busy.set()
                                futures.add(pool.submit(process_job,job))
                            finished={future for future in futures if future.done()}
                            futures.difference_update(finished)
                            for future in finished:
                                try:future.result()
                                except Exception as exc:
                                    print("Worker job error:",type(exc).__name__,
                                          file=sys.stderr,flush=True)
                            if not futures:busy.clear()
                    except KeyboardInterrupt:
                        revoked.set()
                        break
                    except HTTPError as exc:
                        if exc.code==401:
                            print("Device revoked or authentication failed (401). Stopping.",
                                  file=sys.stderr,flush=True)
                            revoked.set()
                            break
                        print("Hub returned HTTP",exc.code,file=sys.stderr,flush=True)
                    except (URLError,TimeoutError,OSError,ValueError) as exc:
                        print("Hub unavailable:",type(exc).__name__,
                              file=sys.stderr,flush=True)
                    if not revoked.is_set():
                        time.sleep(delay)
                        delay=min(30,delay*2)
        finally:
            stopped.set()
            heart.join(timeout=2)

def check_existing(args):
    """Validate local configuration without printing device credentials."""
    cfg, _token, _agent = load_config(Path(args.config).expanduser().resolve())
    print("Existing device credential is usable; device:", cfg["device_id"])
    print("Hub:", cfg["hub_url"])

def main():
    ap=argparse.ArgumentParser(description="ASC Remote Hub outbound device agent")
    sub=ap.add_subparsers(dest="action",required=True)
    en=sub.add_parser("enroll",help="Pair a computer with a user's hub account")
    en.add_argument("--hub",required=True)
    en.add_argument("--name",default=platform.node())
    en.add_argument("--roots",nargs="+",default=[str(Path.home())])
    en.add_argument("--write",action="store_true")
    en.add_argument("--exec",action="store_true")
    en.add_argument("--gui",action="store_true")
    en.add_argument("--delete",action="store_true")
    en.add_argument("--config",default=str(default_config()))
    check=sub.add_parser("check",help="Validate saved credentials without re-enrollment")
    check.add_argument("--config",default=str(default_config()))
    start=sub.add_parser("run",help="Start persistent outbound-only polling")
    start.add_argument("--config",default=str(default_config()))
    args=ap.parse_args()
    (enroll if args.action=="enroll" else (check_existing if args.action=="check" else run))(args)

if __name__=="__main__":
    main()
