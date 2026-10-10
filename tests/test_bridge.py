import contextlib
import io
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"client"))
from bridge import exclusive_agent_lock,busy_heartbeat

class BridgeReliabilityTests(unittest.TestCase):
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
