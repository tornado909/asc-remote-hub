#!/usr/bin/env python3
"""One-shot migration of a stopped hub's mixed traffic trace to per-user/device.

Run only while asc-remote-hub container is stopped, so no writer races occur.
Keeps original mixed source files in a private rollback backup outside the new
traffic-trace directory. Does not print or upload logged payloads.
"""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile
import uuid
from datetime import datetime, timezone

from traffic_trace import TrafficTrace


def _legacy_sources(root: Path) -> list[Path]:
    historical = [
        p for p in root.iterdir()
        if p.is_file() and re.fullmatch(r"traffic\.jsonl\.[0-9]+", p.name)
    ] if root.exists() else []
    historical.sort(key=lambda p: int(p.name.rsplit(".", 1)[-1]), reverse=True)
    current = root / "traffic.jsonl"
    if current.is_file():
        historical.append(current)
    return historical


def _owner_devices(dbfile: Path) -> dict[str, str]:
    with sqlite3.connect(dbfile) as connection:
        return {
            device_id: user_id for device_id, user_id
            in connection.execute("SELECT id,user_id FROM devices")
        }


def _device_from_request(record: dict, owners: dict[str, str]) -> str | None:
    if record.get("route") != "/mcp" or record.get("direction") != "chatgpt_to_hub":
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict) or payload.get("method") != "tools/call":
        return None
    params = payload.get("params")
    if not isinstance(params, dict):
        return None
    if params.get("name") in ("list_devices", "usage_stats", "get_profile"):
        return None
    args = params.get("arguments")
    if not isinstance(args, dict):
        return None
    candidate = args.get("device")
    if isinstance(candidate, str) and owners.get(candidate) == record.get("user_id"):
        return candidate
    return None


def split_legacy(data_dir: Path) -> dict:
    data_dir = Path(data_dir).resolve()
    root = data_dir / "traffic-trace"
    old = _legacy_sources(root)
    if not old:
        return {"migrated": False, "events": 0, "message": "No mixed journal left"}
    if (root / "users").exists():
        raise RuntimeError("Partitioned logs already exist: refusing to duplicate legacy data")
    owners = _owner_devices(data_dir / "hub.sqlite3")

    # First pass: correlate incoming ChatGPT tool calls and replies by
    # authenticated user and generated trace request_id.
    request_device: dict[tuple[str, str], str] = {}
    source_count = 0
    source_hashes: Counter[str] = Counter()
    for source in old:
        with source.open("rb") as handle:
            for raw in handle:
                if not raw.strip():
                    raise ValueError(f"Empty legacy log entry in {source.name}")
                record = json.loads(raw)
                if not isinstance(record, dict):
                    raise ValueError("Invalid legacy trace record")
                user_id = record.get("user_id")
                TrafficTrace._component(user_id)
                device_id = _device_from_request(record, owners)
                if device_id:
                    request_device[(user_id, record.get("request_id"))] = device_id
                source_count += 1
                source_hashes[hashlib.sha256(raw).hexdigest()] += 1

    staging = Path(tempfile.mkdtemp(prefix=".split-stage-", dir=root))
    TrafficTrace._private_dir(staging)
    counts: Counter[str] = Counter()
    try:
        for source in old:
            with source.open("rb") as handle:
                for raw in handle:
                    record = json.loads(raw)
                    user_id = record["user_id"]
                    explicit = record.get("device_id")
                    assigned = (explicit if isinstance(explicit, str) and
                                owners.get(explicit) == user_id else None)
                    if assigned is None and record.get("route") == "/mcp":
                        assigned = request_device.get((user_id, record.get("request_id")))
                    if assigned is not None:
                        TrafficTrace._component(assigned)
                        destination = staging / "users" / user_id / "devices" / assigned / "traffic.jsonl"
                    else:
                        destination = staging / "users" / user_id / "hub" / "traffic.jsonl"
                    ancestor=destination.parent
                    while ancestor != staging:
                        if ancestor.is_symlink():
                            raise ValueError("Migration destination cannot be a symlink")
                        ancestor.mkdir(parents=True,mode=0o700,exist_ok=True)
                        os.chmod(ancestor,0o700)
                        ancestor=ancestor.parent
                    fd = os.open(destination, os.O_CREAT | os.O_APPEND | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
                    with os.fdopen(fd, "ab") as out:
                        out.write(raw)
                        if hasattr(os, "fchmod"):
                os.fchmod(out.fileno(), 0o600)
                    counts[str(destination.relative_to(staging))] += 1
        # Validate every original entry byte-for-byte with a multiset digest.
        dest_hashes: Counter[str] = Counter()
        destination_count = 0
        for path in (staging / "users").rglob("traffic.jsonl"):
            with path.open("rb") as stream:
                for raw in stream:
                    json.loads(raw)
                    dest_hashes[hashlib.sha256(raw).hexdigest()] += 1
                    destination_count += 1
        if destination_count != source_count or dest_hashes != source_hashes:
            raise RuntimeError("Migration verification mismatch: original logs untouched")
        os.replace(staging / "users", root / "users")
        backup = data_dir / (".traffic-trace-legacy-backup-" +
                              datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") +
                              "-" + uuid.uuid4().hex[:8])
        backup.mkdir(mode=0o700, exist_ok=False)
        os.chmod(backup, 0o700)
        for source in old:
            source.replace(backup / source.name)
        result = {
            "migrated": True,
            "events": source_count,
            "buckets": dict(sorted(counts.items())),
            "legacy_backup": str(backup),
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result
    finally:
        shutil.rmtree(staging, ignore_errors=True)


if __name__ == "__main__":
    data = Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/asc-remote-hub/data")
    split_legacy(data)
