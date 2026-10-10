import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

HERE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(HERE/"server"))
from migrate_traffic import split_legacy

class LegacySplitTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data=Path(self.tmp.name)
        self.root=self.data/"traffic-trace"
        self.root.mkdir(mode=0o700)
        with sqlite3.connect(self.data/"hub.sqlite3") as c:
            c.execute("CREATE TABLE devices(id TEXT,user_id TEXT)")
            c.executemany("INSERT INTO devices VALUES (?,?)",
                          [("d_A","u_first"),("d_B","u_first"),
                           ("d_other","u_second")])

    def entry(self,uid,route,direction,request,device=None,payload=None):
        return {
            "utc":"2026-10-10T04:05:00+00:00",
            "request_id":request,"route":route,"direction":direction,
            "user_id":uid,"device_id":device,"http_status":200,
            "payload":payload if payload is not None else {"content":"Привет!\nСкриншот 🖼️ QmFzZTY0"},
        }

    def test_moves_all_legacy_events_and_preserves_bytes(self):
        data=[
            self.entry("u_first","/mcp","chatgpt_to_hub","m1",
                       payload={"method":"tools/call","params":{"name":"machine_info",
                                                              "arguments":{"device":"d_A"}}}),
            self.entry("u_first","/mcp","hub_to_chatgpt","m1"),
            self.entry("u_first","/agent/result","agent_to_hub","a1",device="d_A"),
            self.entry("u_first","/mcp","chatgpt_to_hub","m2",
                       payload={"method":"tools/call","params":{"name":"machine_info",
                                                              "arguments":{"device":"d_B"}}}),
            self.entry("u_first","/mcp","hub_to_chatgpt","m2"),
            self.entry("u_first","/mcp","chatgpt_to_hub","m3",
                       payload={"method":"tools/call","params":{"name":"list_devices",
                                                              "arguments":{}}}),
            self.entry("u_second","/agent/hello","agent_to_hub","o1",device="d_other"),
            self.entry("u_first","/mcp","chatgpt_to_hub","m4",
                       payload={"method":"tools/call","params":{"name":"machine_info",
                                                              "arguments":{"device":"d_other"}}}),
            self.entry("u_first","/mcp","hub_to_chatgpt","m4"),
        ]
        raw=[(json.dumps(x,ensure_ascii=False)+"\n").encode() for x in data]
        # Prior rotation: oldest messages reside in numbered .1 archive.
        (self.root/"traffic.jsonl.1").write_bytes(b"".join(raw[:3]))
        (self.root/"traffic.jsonl").write_bytes(b"".join(raw[3:]))
        with redirect_stdout(io.StringIO()):
            result=split_legacy(self.data)
        self.assertTrue(result["migrated"])
        self.assertEqual(result["events"],len(data))
        def read(p):
            return p.read_bytes().splitlines(keepends=True)
        a=self.root/"users/u_first/devices/d_A/traffic.jsonl"
        b=self.root/"users/u_first/devices/d_B/traffic.jsonl"
        hub=self.root/"users/u_first/hub/traffic.jsonl"
        other=self.root/"users/u_second/devices/d_other/traffic.jsonl"
        self.assertEqual(read(a),raw[:3])
        self.assertEqual(read(b),raw[3:5])
        self.assertEqual(read(hub),raw[5:6]+raw[7:9])
        self.assertEqual(read(other),raw[6:7])
        for p in (a,b,hub,other):
            self.assertEqual(p.stat().st_mode & 0o777,0o600)
            ancestor=p.parent
            while ancestor!=self.root:
                self.assertEqual(ancestor.stat().st_mode & 0o777,0o700)
                ancestor=ancestor.parent
        self.assertFalse((self.root/"traffic.jsonl").exists())
        self.assertFalse((self.root/"traffic.jsonl.1").exists())
        backup=Path(result["legacy_backup"])
        self.assertEqual((backup/"traffic.jsonl.1").read_bytes(),b"".join(raw[:3]))
        self.assertEqual((backup/"traffic.jsonl").read_bytes(),b"".join(raw[3:]))
        self.assertEqual(backup.stat().st_mode & 0o777,0o700)
        with redirect_stdout(io.StringIO()):
            result2=split_legacy(self.data)
        self.assertFalse(result2["migrated"])

    def test_malformed_json_never_moves_source(self):
        (self.root/"traffic.jsonl").write_text("{broken")
        with self.assertRaises(ValueError):
            split_legacy(self.data)
        self.assertTrue((self.root/"traffic.jsonl").exists())
        self.assertFalse((self.root/"users").exists())

    def test_existing_split_logs_blocks_duplicate_import(self):
        (self.root/"traffic.jsonl").write_text("{}\n")
        (self.root/"users").mkdir()
        with self.assertRaises(RuntimeError):
            split_legacy(self.data)
        self.assertTrue((self.root/"traffic.jsonl").exists())
