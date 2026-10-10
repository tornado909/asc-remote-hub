#!/usr/bin/env python3
"""Idempotent migration of historical tool errors from per-device HTTP traces.

Only backfills requests with a matching MCP request and isError response.
The user's own account and device are verified in SQLite. Does not print
arguments, results, tokens, or passwords.
"""
from __future__ import annotations
from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"server"))
from hub import Hub

def restore(data_dir: Path) -> int:
    data_dir=Path(data_dir)
    dbfile=data_dir/"hub.sqlite3"
    conn=sqlite3.connect(dbfile)
    conn.row_factory=sqlite3.Row
    known_users={r["id"] for r in conn.execute("SELECT id FROM users")}
    devices={r["id"]:r["user_id"] for r in conn.execute("SELECT id,user_id FROM devices")}
    conn.execute("""CREATE TABLE IF NOT EXISTS error_events (
      id TEXT PRIMARY KEY,user_id TEXT NOT NULL,created REAL NOT NULL,
      device_id TEXT NOT NULL,tool TEXT NOT NULL,error_type TEXT NOT NULL,
      message TEXT NOT NULL,request_id TEXT,job_id TEXT,source TEXT NOT NULL
    )""")
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_errors_owner_request
                     ON error_events(user_id,request_id) WHERE request_id IS NOT NULL""")
    inserted=0
    scanned=0
    for file in (data_dir/"traffic-trace"/"users").rglob("traffic.jsonl*"):
        if not file.is_file() or file.is_symlink():
            continue
        # The path is derived from local data, not from HTTP input.
        inbound={}
        responses=[]
        with file.open(encoding="utf-8") as reader:
            for line in reader:
                try: r=json.loads(line)
                except json.JSONDecodeError: continue
                uid=r.get("user_id")
                if uid not in known_users or r.get("route")!="/mcp":
                    continue
                token=(uid,r.get("request_id"))
                if not isinstance(token[1],str) or not token[1]:
                    continue
                packet=r.get("payload")
                if not isinstance(packet,dict):
                    continue
                if r.get("direction")=="chatgpt_to_hub" and packet.get("method")=="tools/call":
                    inbound[token]=packet
                elif r.get("direction")=="hub_to_chatgpt":
                    resp=packet.get("result")
                    if isinstance(resp,dict) and resp.get("isError") is True:
                        responses.append((token,r,resp))
        for token,row,result in responses:
            request=inbound.get(token)
            if request is None:
                continue
            scanned+=1
            params=request.get("params") or {}
            if not isinstance(params,dict):
                continue
            name=params.get("name","unknown")
            if not isinstance(name,str):
                continue
            arguments=params.get("arguments") or {}
            if not isinstance(arguments,dict):
                arguments={}
            dev=arguments.get("device")
            dev=dev if isinstance(dev,str) and devices.get(dev)==token[0] else "hub"
            message=Hub.error_text(result)[:3000]
            import re
            prefix,colon,_=message.partition(":")
            kind=prefix if colon and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.]{0,70}",prefix) else "ToolError"
            try:
                created=datetime.fromisoformat(row["utc"]).timestamp()
            except (TypeError,ValueError,KeyError):
                continue
            before=conn.total_changes
            conn.execute("""INSERT OR IGNORE INTO error_events
              (id,user_id,created,device_id,tool,error_type,message,request_id,job_id,source)
              VALUES(?,?,?,?,?,?,?,?,?,?)""",
              ("err_"+uuid.uuid4().hex,token[0],created,dev,name[:100],kind,message,
               token[1],None,"agent" if dev!="hub" else "hub"))
            inserted+=conn.total_changes-before
    conn.commit()
    print(f"Historical error entries: scanned={scanned}, inserted={inserted}")
    return inserted

if __name__=="__main__":
    restore(Path(sys.argv[1] if len(sys.argv)>1 else "/opt/asc-remote-hub/data"))
