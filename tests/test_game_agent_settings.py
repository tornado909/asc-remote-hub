"""Local Game Agent opt-in is atomic, persistent, and off by default."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "client"))
sys.path.insert(0, str(ROOT / "server"))
spec = importlib.util.spec_from_file_location("asc_game_agent_settings", ROOT/"install/game_agent_settings.py")
settings = importlib.util.module_from_spec(spec)
spec.loader.exec_module(settings)


class GameAgentLocalSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/"agent.json"
        self.original = {
            "hub_url":"https://hub.example.test",
            "device_id":"device-fixture",
            "token_file":str(Path(self.tmp.name)/"dpapi-fixture"),
            "roots":[self.tmp.name],
            "permissions":{"write":True,"exec":True,"gui":True,"delete":False},
            "arbitrary_legacy_option":"preserved",
        }
        self.path.write_text(json.dumps(self.original),encoding="utf-8")

    def test_old_install_default_off_and_respects_external_policy(self):
        cfg = settings.read_config(self.path)
        self.assertEqual(settings.effective_policy(cfg),
                         {"enabled":False,"capture":False,"gpu_test":False})
        self.assertEqual(cfg["arbitrary_legacy_option"],"preserved")
        for flag in ("enabled","capture","gpu_test"):
            self.assertFalse(settings.policy_of(cfg)[flag])

    def test_enable_capture_selftest_and_reset_cascade(self):
        cfg = settings.read_config(self.path)
        with self.assertRaisesRegex(RuntimeError,"Enable ASC Game Agent"):
            settings.update_policy(cfg,"capture",True)
        cfg = settings.update_policy(cfg,"enabled",True)
        cfg = settings.update_policy(cfg,"capture",True)
        cfg = settings.update_policy(cfg,"gpu_test",True)
        self.assertTrue(all(settings.effective_policy(cfg).values()))
        settings.write_config(self.path,cfg)
        reread = settings.read_config(self.path)
        self.assertTrue(all(settings.effective_policy(reread).values()))
        reread = settings.update_policy(reread,"enabled",False)
        self.assertEqual(settings.effective_policy(reread),
                         {"enabled":False,"capture":False,"gpu_test":False})
        self.assertEqual(reread["game_agent"],settings.effective_policy(reread))

    def test_missing_permissions_refuse_without_changes(self):
        original_bytes=self.path.read_bytes()
        for permission in ("gui","exec"):
            bad=dict(settings.read_config(self.path))
            bad["permissions"]=dict(bad["permissions"], **{permission: False})
            for flag in ("enabled","capture","gpu_test"):
                with self.subTest(permission=permission,flag=flag):
                    with self.assertRaisesRegex(RuntimeError,"local GUI and execution"):
                        settings.update_policy(bad,flag,True)
        self.assertEqual(self.path.read_bytes(), original_bytes)

    def test_preserves_existing_pairing_credentials_and_file_integrity(self):
        cfg=settings.read_config(self.path)
        new=settings.update_policy(cfg,"enabled",True)
        settings.write_config(self.path,new)
        cfg2=settings.read_config(self.path)
        for key in ("hub_url","device_id","token_file","roots","permissions",
                    "arbitrary_legacy_option"):
            self.assertEqual(cfg2[key],self.original[key])
        self.assertFalse(any(p.name.startswith(".game-agent-") for p in self.path.parent.iterdir()))
        self.assertFalse(self.path.read_bytes().startswith(b"\xef\xbb\xbf"))
        if os.name!="nt":
            self.assertEqual(self.path.stat().st_mode&0o077, 0)

    def test_malformed_flags_refused(self):
        for value in ({'enabled':'yes'}, {'capture':1}, {'bogus':False}, [], "true"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    settings.policy_of(dict(self.original,game_agent=value))
        with self.assertRaises(ValueError):
            settings.update_policy(self.original,"execute",True)

    def test_rejects_symlinks_and_non_config(self):
        with self.assertRaises(RuntimeError):
            settings.read_config(self.path.with_name("not-exist.json"))
        symlink=self.path.with_name("link.json")
        try:
            symlink.symlink_to(self.path)
        except OSError:
            return
        with self.assertRaisesRegex(RuntimeError,"regular"):
            settings.read_config(symlink)
        with self.assertRaisesRegex(RuntimeError,"symlink"):
            settings.write_config(symlink,self.original)

    def test_backend_load_config_fails_closed_even_if_env_opted_in(self):
        import bridge
        self.path.with_name("dpapi-fixture").write_text("x"*50,encoding="utf8")
        with patch.dict(os.environ,{
            "ASC_RC_ALLOW_GAME_AGENT":"1",
            "ASC_RC_ALLOW_GAME_AGENT_CAPTURE":"1",
            "ASC_RC_ALLOW_GAME_AGENT_GPU_TEST":"1",
            "ASC_RC_ALLOW_GUI":"1","ASC_RC_ALLOW_EXEC":"1"
        }):
            _conf,_token,agent=bridge.load_config(self.path)
            # In Linux CI Game Agent must never activate, even if a hostile
            # inherited process environment requested it.
            self.assertEqual(os.environ["ASC_RC_ALLOW_GAME_AGENT"],"0")
            self.assertEqual(os.environ["ASC_RC_ALLOW_GAME_AGENT_CAPTURE"],"0")
            self.assertEqual(os.environ["ASC_RC_ALLOW_GAME_AGENT_GPU_TEST"],"0")
            self.assertNotIn("game_agent_status",agent.tools)

    def test_public_package_and_tray_are_wired(self):
        from distribution import ALLOWED_FILES
        self.assertIn("install/game_agent_settings.py",ALLOWED_FILES)
        tray=(ROOT/"install/tray_windows.ps1").read_text("utf-8-sig")
        for marker in ("game_agent_settings.py","Включить Game Agent",
                       "Разрешить GPU-самотест","Разрешить запрос захвата окна",
                       "Stop-AgentWorker","Start-AgentWorker"):
            self.assertIn(marker,tray)


if __name__=="__main__":
    unittest.main()
