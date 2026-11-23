#!/usr/bin/env python3
"""Install the unmodified, pinned Windows agent-desktop native binary.

No Node.js, npm, inference service or secondary agent. Only one 4 MB EXE.
The public archive and the extracted EXE are both verified with SHA-256.
Run explicitly on a Windows device; existing ASC pairing is preserved.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import os
from pathlib import Path
import sys
import tarfile
import zipfile
import tempfile
from urllib.request import Request, urlopen

HERE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(HERE/"client"))
from asc_desktop_native import WINDOWS_EXE_SHA256, installed, native_executable
from asc_desktop_ffi import DLL_SHA256, DLL_ARCHIVE_SHA256, available as ffi_available, library_path

ARCHIVE="agent-desktop-v1.3.1-x86_64-pc-windows-msvc.tar.gz"
ARCHIVE_SHA256="6ee4b1e078f4e78792e79f16e32e4a78b9919464aa3c55629b7e27ed53a08c13"
SOURCE="https://github.com/vercel-labs/agent-desktop/releases/download/v1.3.1/"+ARCHIVE
MAX_ARCHIVE=5_000_000
MAX_BINARY=12_000_000


def install(archive_file:Path|None=None) -> Path:
    if os.name!="nt":
        raise RuntimeError("The pinned native distribution is Windows x64 only")
    if installed():
        return native_executable()
    if archive_file is not None:
        raw=archive_file.read_bytes()
    else:
        request=Request(SOURCE,headers={"User-Agent":"ASC-Remote-Hub-Desktop-Installer"})
        with urlopen(request,timeout=40) as response:
            raw=response.read(MAX_ARCHIVE+1)
    if len(raw)>MAX_ARCHIVE or hashlib.sha256(raw).hexdigest()!=ARCHIVE_SHA256:
        raise ValueError("Native desktop archive failed SHA256 verification")
    with tarfile.open(fileobj=io.BytesIO(raw),mode="r:gz") as tar:
        binaries=[
            member for member in tar.getmembers()
            if member.name.split("/")[-1].casefold()=="agent-desktop.exe"
            and member.isfile()
        ]
        if len(binaries)!=1 or binaries[0].size>MAX_BINARY:
            raise ValueError("Native desktop binary not uniquely identifiable")
        handle=tar.extractfile(binaries[0])
        if handle is None:
            raise ValueError("Native binary missing from the archive")
        executable=handle.read(MAX_BINARY+1)
    if len(executable)>MAX_BINARY or hashlib.sha256(executable).hexdigest()!=WINDOWS_EXE_SHA256:
        raise ValueError("Extracted native binary failed SHA256 verification")
    destination=native_executable()
    destination.parent.mkdir(parents=True,exist_ok=True)
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(
                mode="wb",prefix=".verified-agent-desktop-",
                suffix=".tmp",dir=destination.parent,delete=False) as output:
            temporary=Path(output.name)
            output.write(executable)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary,destination)
        temporary=None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    if not installed():
        raise ValueError("Installed native desktop binary verification failed")
    return destination


FFI_ARCHIVE="agent-desktop-ffi-v1.3.1-x86_64-pc-windows-msvc.zip"
FFI_SOURCE="https://github.com/vercel-labs/agent-desktop/releases/download/v1.3.1/"+FFI_ARCHIVE
MAX_FFI_ARCHIVE=4_000_000


def install_ffi(archive_file:Path|None=None)->Path:
    """Opt-in pinned C library. Never used unless a caller requests engine=ffi."""
    if os.name!="nt":
        raise RuntimeError("Windows x64 only")
    if ffi_available():
        return library_path()
    if archive_file is not None:
        raw=archive_file.read_bytes()
    else:
        request=Request(FFI_SOURCE,headers={"User-Agent":"ASC-Remote-Hub-Desktop-Installer"})
        with urlopen(request,timeout=40) as response:
            raw=response.read(MAX_FFI_ARCHIVE+1)
    if len(raw)>MAX_FFI_ARCHIVE or hashlib.sha256(raw).hexdigest()!=DLL_ARCHIVE_SHA256:
        raise ValueError("Native FFI archive failed SHA256 verification")
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        matches=[entry for entry in z.infolist()
                 if Path(entry.filename).name.casefold()=="agent_desktop_ffi.dll"
                 and not entry.is_dir()]
        if len(matches)!=1 or matches[0].file_size>MAX_BINARY:
            raise ValueError("Native FFI DLL not uniquely identifiable")
        payload=z.read(matches[0])
    if len(payload)>MAX_BINARY or hashlib.sha256(payload).hexdigest()!=DLL_SHA256:
        raise ValueError("Native FFI DLL failed SHA256 verification")
    dest=library_path()
    dest.parent.mkdir(parents=True,exist_ok=True)
    tmp=None
    try:
        with tempfile.NamedTemporaryFile(mode="wb",prefix=".verified-desktop-ffi-",
                                         suffix=".tmp",dir=dest.parent,delete=False) as output:
            tmp=Path(output.name)
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp,dest)
        tmp=None
    finally:
        if tmp is not None:tmp.unlink(missing_ok=True)
    if not ffi_available():
        raise ValueError("Installed FFI DLL verification failed")
    return dest


if __name__=="__main__":
    parser=argparse.ArgumentParser(description="Verify and install native semantic desktop support")
    parser.add_argument("--archive",type=Path,help="Use already downloaded official release archive")
    parser.add_argument("--check",action="store_true",help="Verify existing binary, make no changes")
    parser.add_argument("--ffi",action="store_true",help="Install optional, SHA256-pinned C API DLL")
    args=parser.parse_args()
    if args.check:
        ok=ffi_available() if args.ffi else installed()
        print("Native desktop verified" if ok else "Native desktop unavailable")
        sys.exit(0 if ok else 1)
    path=install_ffi(args.archive) if args.ffi else install(args.archive)
    print("Verified native semantic desktop installed:",path)
