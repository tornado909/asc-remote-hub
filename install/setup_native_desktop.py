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
import tempfile
from urllib.request import Request, urlopen

HERE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(HERE/"client"))
from asc_desktop_native import WINDOWS_EXE_SHA256, installed, native_executable

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


if __name__=="__main__":
    parser=argparse.ArgumentParser(description="Verify and install native semantic desktop support")
    parser.add_argument("--archive",type=Path,help="Use already downloaded official release archive")
    parser.add_argument("--check",action="store_true",help="Verify existing binary, make no changes")
    args=parser.parse_args()
    if args.check:
        print("Native desktop installed and verified" if installed() else "Native desktop unavailable")
        sys.exit(0 if installed() else 1)
    path=install(args.archive)
    print("Verified native semantic desktop installed:",path)
