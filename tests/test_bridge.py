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


class WindowsCredentialTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform=="win32","Windows-only DPAPI")
    def test_native_dpapi_unicode_roundtrip(self):
        from bridge import encrypt_secret,decrypt_secret
        for sample in ("ASC-HUB-TEST-12345", "Русский текст и emoji 🙂"):
            encrypted=encrypt_secret(sample)
            self.assertTrue(encrypted.startswith("dpapi1:"))
            self.assertNotIn(sample,encrypted)
            self.assertEqual(decrypt_secret(encrypted),sample)

    @unittest.skipUnless(sys.platform=="win32","Windows-only legacy credentials")
    def test_existing_legacy_windows_key_remains_readable(self):
        import subprocess
        from bridge import decrypt_secret
        sample="ASC-HUB-LEGACY-TEST-NONSECRET"
        script=("$ErrorActionPreference='Stop';"
                "$plain=[Console]::In.ReadToEnd();"
                "$sec=ConvertTo-SecureString -String $plain -AsPlainText -Force;"
                "[Console]::Out.Write((ConvertFrom-SecureString $sec))")
        process=subprocess.run(["powershell.exe","-NoProfile","-NonInteractive","-Command",script],
                               input=sample.encode(),capture_output=True,timeout=20,check=True)
        self.assertEqual(decrypt_secret(process.stdout.decode().strip()), sample)

    def test_enrollment_preflight_runs_before_one_time_code_redemption(self):
        import os
        from types import SimpleNamespace
        from unittest.mock import patch
        from bridge import enroll
        with tempfile.TemporaryDirectory() as temp:
            args=SimpleNamespace(hub="https://hub.example.test",name="test-pc",
                config=str(Path(temp)/"agent.json"),roots=[temp],write=False,
                exec=False,gui=False,delete=False)
            with patch("bridge.Agent"),patch("bridge.request") as network, \
                 patch("bridge.preflight_credentials",side_effect=RuntimeError("DPAPI unavailable")), \
                 patch.dict(os.environ,{"ASC_HUB_PAIRING_CODE":"TEST-CODE"}):
                with self.assertRaisesRegex(RuntimeError,"DPAPI unavailable"):
                    enroll(args)
                network.assert_not_called()

    def test_preflight_does_not_leave_temporary_secret(self):
        from bridge import preflight_credentials
        with tempfile.TemporaryDirectory() as temp:
            target=Path(temp)/"creds"/"agent.json"
            preflight_credentials(target)
            self.assertEqual(list(target.parent.iterdir()), [])

