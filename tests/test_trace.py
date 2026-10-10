import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
from traffic_trace import TrafficTrace
from hub import Hub, Handler, ThreadingHTTPServer, digest


class TrafficTraceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def test_off_by_default_creates_no_logs(self):
        t = TrafficTrace(self.base)
        t.record(direction="chatgpt_to_hub", route="/mcp", user_id="u1",
                 device_id=None, request_id="test", payload={"x": "secret"})
        self.assertFalse(t.directory.exists())

    def test_only_intended_account_full_unicode_and_binary(self):
        t = TrafficTrace(self.base, enabled=True, account_id="u1")
        payload = {
            "jsonrpc": "2.0",
            "params": {
                "name": "type_text", "arguments": {
                    "text": "Привет!\nСтрока 2 🚀\n",
                    "image_base64": "QmFzZTY0RGF0YQ==" * 400,
                },
            },
        }
        t.record(direction="chatgpt_to_hub", route="/mcp", user_id="u2",
                 device_id=None, request_id="not-owner", payload=payload)
        self.assertFalse(t.path_for('u1',None).exists())
        t.record(direction="chatgpt_to_hub", route="/mcp", user_id="u1",
                 device_id=None, request_id="matched", payload=payload)
        journal=t.path_for("u1",None)
        row = json.loads(journal.read_text(encoding="utf-8"))
        self.assertEqual(row["payload"], payload)
        self.assertEqual(row["request_id"], "matched")
        # NTFS ACLs are not represented by POSIX chmod mode bits on Windows.
        if os.name != "nt":
            self.assertEqual(os.stat(t.directory).st_mode & 0o777, 0o700)
            self.assertEqual(os.stat(journal).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(journal.parent).st_mode & 0o777, 0o700)

    def test_rotates_on_size_limit_and_never_truncates_entry(self):
        t = TrafficTrace(self.base, enabled=True, account_id="u1", max_bytes=1024, backups=2)
        for i in range(5):
            payload = {"marker": i, "text": "X" * 1800}
            t.record(direction="agent_to_hub", route="/agent/result",
                     user_id="u1", device_id="d_1", request_id=str(i),
                     payload=payload)
        journal=t.path_for("u1","d_1")
        self.assertTrue(journal.exists())
        self.assertTrue(Path(str(journal) + ".1").exists())
        self.assertTrue(Path(str(journal) + ".2").exists())
        self.assertFalse(Path(str(journal) + ".3").exists())
        latest = json.loads(journal.read_text())
        self.assertEqual(latest["payload"]["text"], "X" * 1800)
        self.assertEqual(latest["payload"]["marker"], 4)

    def test_per_device_separation_rotation_and_hub_bucket(self):
        t=TrafficTrace(self.base,enabled=True,account_id="u1",max_bytes=1024,backups=1)
        t.record(direction="hub_to_agent",route="/agent/next",user_id="u1",
                 device_id="d_linux",request_id="linux1",payload={"text":"Linux-only"})
        t.record(direction="hub_to_agent",route="/agent/next",user_id="u1",
                 device_id="d_win",request_id="win1",payload={"text":"Windows-only"})
        t.record(direction="hub_to_chatgpt",route="/mcp",user_id="u1",
                 device_id=None,request_id="hub1",payload={"kind":"list_devices"})
        for suffix,device in (("linux","d_linux"),("win","d_win")):
            journal=t.path_for("u1",device)
            entry=json.loads(journal.read_text())
            self.assertEqual(entry["request_id"],suffix+"1")
            self.assertNotIn("Windows-only" if suffix=="linux" else "Linux-only",
                             journal.read_text())
        self.assertIn("hub1",t.path_for("u1",None).read_text())
        t.record(direction="hub_to_agent",route="/agent/next",user_id="u1",
                 device_id="d_linux",request_id="linux2",payload={"text":"T"*2000})
        self.assertTrue(Path(str(t.path_for("u1","d_linux"))+".1").exists())
        self.assertFalse(Path(str(t.path_for("u1","d_win"))+".1").exists())

    def test_path_traversal_rejected_for_trace_components(self):
        t=TrafficTrace(self.base,enabled=True,account_id="u1")
        for bad in ("../outside","../../etc/passwd","d/a","", "."):
            with self.assertRaises(ValueError):
                t.path_for("u1",bad)
        with self.assertRaises(ValueError):
            t.path_for("../outside",None)

    def test_required_scope(self):
        with self.assertRaises(ValueError):
            TrafficTrace(self.base, enabled=True, account_id="")

    def test_end_to_end_http_traces_only_authenticated_tool_data(self):
        folder = self.base / "hub"
        hub = Hub(base_url="http://127.0.0.1:9876", data_dir=str(folder), test_mode=True)
        u1 = hub.register_user("owner@example.org", "MyStrongTestPassword2026")
        u2 = hub.register_user("other@example.org", "MyOtherTestPassword2026")
        hub.trace = TrafficTrace(folder, enabled=True, account_id=u1["id"])
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        http = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        http.daemon_threads = True
        http.hub = hub
        bg = threading.Thread(target=http.serve_forever, daemon=True)
        bg.start()
        self.addCleanup(lambda: (http.shutdown(), http.server_close(), bg.join(timeout=4)))
        base = f"http://127.0.0.1:{port}"

        def post(path, data, headers):
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            req = Request(base + path, data=body, method="POST",
                          headers={"Content-Type": "application/json", **headers})
            with urlopen(req, timeout=6) as resp:
                return json.loads(resp.read().decode("utf-8"))

        for user, value in ((u1, "token-owner"), (u2, "token-other")):
            access = value * 8
            hub.execute(
                "INSERT INTO oauth_tokens(hash,user_id,client_id,kind,resource,scopes,expires) VALUES(?,?,?,?,?,?,?)",
                (digest(access), user["id"], "testclient", "access", hub.resource, "remote.access", time.time()+3600)
            )
            answer = post("/mcp", {
                "jsonrpc": "2.0", "id": 11, "method": "tools/call",
                "params": {"name": "list_devices", "arguments": {}}
            }, {"Authorization": "Bearer " + access})
            self.assertEqual(answer["id"], 11)
        # Check that OAuth tokens and another tenant's call are NOT in trace
        hub_journal=hub.trace.path_for(u1["id"],None)
        trace_lines = [json.loads(x) for x in hub_journal.read_text().splitlines()]
        self.assertEqual(len(trace_lines), 2)
        self.assertEqual([x["direction"] for x in trace_lines],
                         ["chatgpt_to_hub", "hub_to_chatgpt"])
        self.assertEqual(trace_lines[0]["request_id"], trace_lines[1]["request_id"])
        self.assertTrue(all(x["user_id"] == u1["id"] for x in trace_lines))
        self.assertTrue(all("token-owner" not in json.dumps(x) for x in trace_lines))

        code = hub.pairing_create(u1["id"])
        paired = hub.pairing_redeem(code, "Test Windows", "Windows", ["machine_info"])
        reply = post("/agent/hello", {
            "machine_info": {"name": "Test Windows", "platform": "Windows"},
            "tools": ["machine_info"], "version": "0.3"
        }, {"Authorization": "Bearer " + paired["device_token"],
            "X-ASC-Device": paired["device_id"]})
        self.assertTrue(reply["ok"])
        device_journal=hub.trace.path_for(u1["id"],paired["device_id"])
        trace_lines = [json.loads(x) for x in device_journal.read_text().splitlines()]
        self.assertEqual(len(trace_lines), 2)
        self.assertEqual(trace_lines[-2]["route"], "/agent/hello")
        self.assertEqual(trace_lines[-1]["http_status"], 200)
        self.assertNotIn(paired["device_token"], device_journal.read_text())
        self.assertFalse(hub.trace.path_for(u2["id"],None).exists())

    def test_mcp_tool_call_journal_follows_authenticated_owner_device(self):
        folder=self.base/"routing"
        hub=Hub(base_url="http://127.0.0.1:9876",data_dir=str(folder),test_mode=True)
        user=hub.register_user("routing@example.org","RoutingTestPassword2026")
        other=hub.register_user("other-routing@example.org","OtherRoutingPassword2026")
        hub.trace=TrafficTrace(folder,enabled=True,account_id=user["id"])
        own_code=hub.pairing_create(user["id"])
        own=hub.pairing_redeem(own_code,"Own","Windows",["machine_info"])
        outsider_code=hub.pairing_create(other["id"])
        outsider=hub.pairing_redeem(outsider_code,"Other","Linux",["machine_info"])
        sock=socket.socket()
        sock.bind(("127.0.0.1",0))
        port=sock.getsockname()[1]
        sock.close()
        http=ThreadingHTTPServer(("127.0.0.1",port),Handler)
        http.daemon_threads=True
        http.hub=hub
        thread=threading.Thread(target=http.serve_forever,daemon=True)
        thread.start()
        self.addCleanup(lambda:(http.shutdown(),http.server_close(),thread.join(timeout=3)))
        access="routed-token-example-long-enough-1234567890"
        hub.execute(
            "INSERT INTO oauth_tokens(hash,user_id,client_id,kind,resource,scopes,expires) VALUES(?,?,?,?,?,?,?)",
            (digest(access),user["id"],"client","access",hub.resource,"remote.access",time.time()+3600))
        def call(device):
            body=json.dumps({"jsonrpc":"2.0","id":7,"method":"tools/call",
                             "params":{"name":"machine_info","arguments":{"device":device}}}).encode()
            req=Request(f"http://127.0.0.1:{port}/mcp",data=body,
                        headers={"Content-Type":"application/json","Authorization":"Bearer "+access})
            with urlopen(req,timeout=5) as resp:
                return json.loads(resp.read())
        self.assertEqual(call(own["device_id"])["result"]["isError"],True)
        own_journal=hub.trace.path_for(user["id"],own["device_id"])
        entries=[json.loads(x) for x in own_journal.read_text().splitlines()]
        self.assertEqual(len(entries),2)
        self.assertEqual(entries[0]["request_id"],entries[1]["request_id"])
        self.assertEqual(entries[0]["device_id"],own["device_id"])
        self.assertEqual(call(outsider["device_id"])["result"]["isError"],True)
        self.assertFalse(hub.trace.path_for(user["id"],outsider["device_id"]).exists())
        hub_entries=hub.trace.path_for(user["id"],None).read_text().splitlines()
        self.assertEqual(len(hub_entries),2)
        self.assertFalse(hub.trace.path_for(other["id"],None).exists())
