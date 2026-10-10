"""Build public, domain-specific install packages from vetted source files.

The public archive is explicitly allowlisted; no .env, database, logs,
user-specific backups, or arbitrary files can enter it. No npm registry,
Docker-in-Docker, external key, or Node.js needed on the server.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tarfile
import tempfile

RELEASE = "1.0.1"
CLI = f"cli-{RELEASE}.tgz"
AGENT = f"asc-remote-hub-agent-{RELEASE}.tar.gz"
ALLOWED_FILES = (
    "client/asc_remote_commander.py",
    "client/bridge.py",
    "install/start_windows.ps1",
    "install/install_windows.ps1",
    "install/install_linux.sh",
    "LICENSE",
)
PACKAGE_NAME = "asc-remote-hub-cli"


def _tar_bytes(entries: dict[str, tuple[bytes, int]]) -> bytes:
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0, filename="") as gz:
        with tarfile.open(fileobj=gz, mode="w") as tar:
            for name in sorted(entries):
                raw, mode = entries[name]
                item = tarfile.TarInfo(name)
                item.size = len(raw)
                item.mode = mode
                item.uid = item.gid = 0
                item.uname = item.gname = ""
                item.mtime = 0
                tar.addfile(item, io.BytesIO(raw))
    return out.getvalue()


def _safe_output(dir: Path, name: str, content: bytes) -> Path:
    dest = dir / name
    fd, temporary = tempfile.mkstemp(prefix=".asc-dist-", suffix=".tmp", dir=dir)
    try:
        if hasattr(os, "fchmod"):\n            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, dest)
        return dest
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def build_distributions(project_root: Path, data_dir: Path, origin: str) -> dict[str, Path]:
    if not (origin.startswith("https://") or origin.startswith("http://127.0.0.1:")):
        raise ValueError("A public distribution requires HTTPS.")
    if not re.fullmatch(r"https?://[A-Za-z0-9._:-]+", origin):
        raise ValueError("Invalid origin in package generation")
    root = Path(project_root).resolve()
    output = Path(data_dir) / "public"
    if output.is_symlink():
        raise ValueError("Public archive output may not be a symlink")
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(output, 0o700)

    # Explicit list is the only acceptable source for public files.
    agent_entries = {}
    for rel in ALLOWED_FILES:
        f = root / rel
        if not f.is_file() or f.is_symlink():
            raise ValueError("Missing or symlinked public source file: " + rel)
        agent_entries["asc-remote-hub/" + rel] = (f.read_bytes(), 0o644)
    install_guide = (
        "# ASC Remote Hub Agent\\n\\n"
        "Use the installation command displayed on your hub's website.\\n"
        "Connect with a one-time pairing code created in your own account.\\n"
        "Runs over outbound HTTPS. No paid relay service is required.\\n"
        "Your hub administrator controls the public server.\\n"
    ).replace("\\n", "\n")
    agent_entries["asc-remote-hub/README.md"] = (install_guide.encode(), 0o644)
    agent_raw = _tar_bytes(agent_entries)
    sha = hashlib.sha256(agent_raw).hexdigest()

    template_path = root / "server" / "installer_cli" / "connect.cjs"
    if not template_path.is_file() or template_path.is_symlink():
        raise ValueError("Missing CLI template")
    template = template_path.read_text(encoding="utf-8")
    if template.count("__HUB_ORIGIN__") != 1 or template.count("__ARCHIVE_SHA256__") != 1:
        raise ValueError("CLI template markers missing or duplicated")
    cli_js = template.replace("'__HUB_ORIGIN__'", json.dumps(origin))
    cli_js = cli_js.replace("__ARCHIVE_SHA256__", sha)
    if "__HUB_" in cli_js or "__ARCHIVE_" in cli_js:
        raise ValueError("Unexpanded CLI marker")
    manifest = {
        "name": PACKAGE_NAME,
        "version": RELEASE,
        "license": "MIT",
        "description": "Self-hosted ChatGPT MCP device bridge setup for Windows and Linux",
        "bin": {"asc-remote-hub": "bin/connect.cjs"},
        "engines": {"node": ">=18"},
        "os": ["win32", "linux"],
        "files": ["bin/connect.cjs", "README.md"]
    }
    cli_guide = (
        "# ASC Remote Hub CLI\\n\\n"
        f"Install: npx --yes {origin}/{CLI}\\n"
        "The CLI requests a one-time pairing code from your account.\\n"
        "No npm registry publishing, platform API key, or paid tunnel required.\\n"
    ).replace("\\n", "\n")
    cli_raw = _tar_bytes({
        "package/package.json": (json.dumps(manifest, ensure_ascii=False, indent=2).encode() + b"\n", 0o644),
        "package/bin/connect.cjs": (cli_js.encode("utf-8"), 0o755),
        "package/README.md": (cli_guide.encode(), 0o644),
    })

    # Preserve an existing file if identical (avoids needless inode churn).
    result = {}
    for name, raw in ((AGENT, agent_raw), (CLI, cli_raw)):
        dest = output / name
        if not dest.is_file() or hashlib.sha256(dest.read_bytes()).digest() != hashlib.sha256(raw).digest():
            _safe_output(output, name, raw)
        result[name] = dest
    return result
