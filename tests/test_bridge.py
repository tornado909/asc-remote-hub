import contextlib
import io
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"client"))
from bridge import exclusive_agent_lock,busy_heartbeat,record_connected_marker,load_config,VERSION

class BridgeReliabilityTests(unittest.TestCase):
    def test_config_loads_utf8_with_bom_and_preserves_roots(self):
        import json
        import os
        from unittest import mock
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp)
            config=directory/"agent.json"
            token=directory/"device-key.secret"
            token.write_text("A"*48,encoding="utf-8")
            roots=[str(directory),str(directory/"project")]
            (directory/"project").mkdir()
            config.write_text(json.dumps({
                "hub_url":"https://private-deployment.example.invalid",
                "device_id":"d_test","token_file":str(token),
                "name":"Test Device","roots":roots,
                "permissions":{"write":True,"exec":True,"gui":False,"delete":False}
            }),encoding="utf-8-sig")
            with mock.patch.dict(os.environ, {}, clear=False), mock.patch(
                    "bridge.decrypt_secret", return_value="A"*48):
                loaded,secret,agent=load_config(config)
                self.assertEqual(loaded["roots"],roots)
                self.assertEqual(secret,"A"*48)
                self.assertEqual(os.environ["ASC_RC_ROOTS"],";".join(roots))


    def test_ready_marker_contains_no_credentials_and_is_atomic(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)
            self.assertTrue(record_connected_marker("device-test",directory=folder))
            data=json.loads((folder/"agent.ready.json").read_text())
            self.assertEqual(data["device_id"],"device-test")
            self.assertEqual(data["version"],VERSION)
            self.assertGreater(data["pid"],0)
            self.assertGreater(data["connected_at"],0)
            self.assertEqual(set(data),{"device_id","version","pid","connected_at"})
            self.assertTrue(record_connected_marker("device-test2",directory=folder))
            self.assertEqual(json.loads((folder/"agent.ready.json").read_text())["device_id"],"device-test2")
            self.assertEqual(len(list(folder.iterdir())),1)

    def test_exclusive_lock_blocks_duplicate_in_same_account(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg=Path(temp)/"agent.json"
            with exclusive_agent_lock(cfg):
                with self.assertRaises(RuntimeError):
                    with exclusive_agent_lock(cfg):
                        self.fail("duplicate agent acquired lock")
            with exclusive_agent_lock(cfg):
                pass
            self.assertTrue((cfg.parent/"agent.lock").exists())

    def test_heartbeat_runs_only_during_busy_operation(self):
        busy=threading.Event()
        stop=threading.Event()
        revoked=threading.Event()
        seen=[]
        t=threading.Thread(target=busy_heartbeat,
              args=(busy,stop,revoked,lambda:seen.append(time.monotonic()),0.02),
              daemon=True)
        t.start()
        time.sleep(0.07)
        self.assertEqual(len(seen),0)
        busy.set()
        time.sleep(0.10)
        busy.clear()
        emitted=len(seen)
        self.assertGreaterEqual(emitted,2)
        time.sleep(0.06)
        self.assertEqual(len(seen),emitted)
        stop.set()
        t.join(2)
        self.assertFalse(t.is_alive())
        self.assertFalse(revoked.is_set())

    def test_heartbeat_rejecting_auth_stops_worker(self):
        from urllib.error import HTTPError
        busy=threading.Event()
        busy.set()
        stop=threading.Event()
        revoked=threading.Event()
        called=[]
        def sender():
            called.append(1)
            raise HTTPError("https://example.test",401,"no auth",{},None)
        with contextlib.redirect_stderr(io.StringIO()):
            t=threading.Thread(target=busy_heartbeat,
                args=(busy,stop,revoked,sender,0.015),daemon=True)
            t.start()
            t.join(1)
        self.assertTrue(revoked.is_set())
        self.assertFalse(t.is_alive())
        self.assertEqual(len(called),1)
