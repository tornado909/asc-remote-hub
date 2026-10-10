from __future__ import annotations

import json
import os
from pathlib import Path
import re
import threading
from datetime import datetime, timezone

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class TrafficTrace:
    """Opt-in full-payload trace, partitioned by user and device.

    Only the account selected by ASC_HUB_TRACE_ACCOUNT is collected.
    Authentication endpoints and credentials are never captured.
    Every active stream has its own rotation and lock-protected writes.
    """

    def __init__(self, data_dir: Path, *, enabled: bool = False,
                 account_id: str = "", max_bytes: int = 64 * 1024 * 1024,
                 backups: int = 4) -> None:
        if enabled and not account_id:
            raise ValueError("Full traffic tracing requires ASC_HUB_TRACE_ACCOUNT")
        self.enabled = enabled
        self.account_id = account_id
        self.max_bytes = max(1024, int(max_bytes))
        self.backups = min(12, max(1, int(backups)))
        self.directory = Path(data_dir) / "traffic-trace"
        self.users_directory = self.directory / "users"
        self.lock = threading.RLock()
        if enabled:
            self._private_dir(self.directory)

    @staticmethod
    def _component(value: str) -> str:
        if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
            raise ValueError("Unsafe user or device identifier in traffic trace")
        return value

    @staticmethod
    def _private_dir(path: Path) -> None:
        if path.is_symlink():
            raise ValueError("Traffic trace path may not be a symlink")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink():
            raise ValueError("Traffic trace path may not be a symlink")
        os.chmod(path, 0o700)

    def path_for(self, user_id: str, device_id: str | None) -> Path:
        uid = self._component(user_id)
        base = self.users_directory / uid
        if device_id is None:
            return base / "hub" / "traffic.jsonl"
        return base / "devices" / self._component(device_id) / "traffic.jsonl"

    def _rotate(self, path: Path) -> None:
        oldest = path.with_name(path.name + "." + str(self.backups))
        if oldest.exists():
            oldest.unlink()
        for i in range(self.backups - 1, 0, -1):
            source = path.with_name(path.name + "." + str(i))
            if source.exists():
                source.replace(path.with_name(path.name + "." + str(i + 1)))
        if path.exists():
            path.replace(path.with_name(path.name + ".1"))

    def record(self, *, direction: str, route: str, user_id: str,
               device_id: str | None, request_id: str,
               payload: object, status: int | None = None) -> None:
        if not self.enabled or user_id != self.account_id:
            return
        record = {
            "utc": datetime.now(timezone.utc).isoformat(),
            "request_id": request_id,
            "route": route,
            "direction": direction,
            "user_id": user_id,
            "device_id": device_id,
            "http_status": status,
            "payload": payload,
        }
        try:
            line = (json.dumps(record, ensure_ascii=False, separators=(",", ":"),
                               allow_nan=False) + "\n").encode("utf-8")
            path = self.path_for(user_id, device_id)
            with self.lock:
                self._private_dir(self.users_directory)
                self._private_dir(self.users_directory / user_id)
                if device_id is not None:
                    self._private_dir(self.users_directory / user_id / "devices")
                self._private_dir(path.parent)
                size = path.stat().st_size if path.exists() else 0
                if size and size + len(line) > self.max_bytes:
                    self._rotate(path)
                flags = os.O_CREAT | os.O_APPEND | os.O_WRONLY
                flags |= getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(path, flags, 0o600)
                with os.fdopen(fd, "ab") as out:
                    os.fchmod(out.fileno(), 0o600)
                    out.write(line)
        except (OSError, ValueError, TypeError) as exc:
            # Trace failures do not prevent the actual MCP/agent operation.
            # No payload, secrets, or trace content are printed to stdio.
            print("ASC traffic trace write error:", type(exc).__name__, flush=True)
