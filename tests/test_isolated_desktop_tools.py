"""ASC GUI Engine: contract, gating and fail-closed tests on any CI host.

Native CreateDesktop / PrintWindow / WM_* integration runs separately on Windows.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"client"))
import asc_remote_commander as module

NAMES={
 "gui_desktop_create","gui_desktop_sessions","gui_desktop_launch",
 "gui_desktop_windows","gui_desktop_action","gui_desktop_capture","gui_desktop_close",
}

class IsolatedDesktopToolTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder=Path(tmp.name)
        env=mock.patch.dict(os.environ,{
            "ASC_RC_ROOTS":tmp.name,
            "ASC_RC_STATE_DIR":str(self.folder/"state"),
            "ASC_RC_ALLOW_GUI":"1",
            "ASC_RC_ALLOW_EXEC":"1",
            "ASC_RC_ALLOW_WRITE":"0",
            "ASC_RC_ALLOW_DELETE":"0"},clear=False)
        env.start()
        self.addCleanup(env.stop)
        # Simulate native Windows solely for schema registration. Never import
        # the actual ctypes Win32 backend in cross-platform unit tests.
        fake=types.SimpleNamespace(**vars(os))
        fake.name="nt"
        patch=mock.patch.object(module,"os",fake)
        patch.start()
        self.addCleanup(patch.stop)
        self.agent=module.Agent()

    def test_tools_and_capability_annotations(self):
        self.assertTrue(NAMES.issubset(self.agent.tools))
        self.assertEqual(module.VERSION,"1.1.4")
        for name in NAMES:
            schema=self.agent.tools[name][0]["inputSchema"]
            self.assertFalse(schema["additionalProperties"])
        self.assertTrue(self.agent.tools["gui_desktop_close"][0]["annotations"]["destructiveHint"])
        self.assertFalse(self.agent.tools["gui_desktop_create"][0]["annotations"]["readOnlyHint"])

    def test_missing_gui_or_exec_cannot_create_manager(self):
        self.agent.allow_gui=False
        with self.assertRaises(module.AccessDenied):
            self.agent._isolated()
        self.agent.allow_gui=True
        self.agent.allow_exec=False
        with self.assertRaises(module.AccessDenied):
            self.agent._isolated()

    def test_inaccessible_mutation_tools_not_registered(self):
        for env in ({"ASC_RC_ALLOW_GUI":"0"}, {"ASC_RC_ALLOW_EXEC":"0"}):
            with self.subTest(env=env):
                with mock.patch.dict(os.environ,env):
                    agent=module.Agent()
                    self.assertFalse(NAMES.intersection(agent.tools))

    def test_launch_rejects_non_absolute_and_malformed_arguments(self):
        self.agent._isolated_desktops=mock.Mock()
        for args in ({"desktop_id":"id","program":"notepad.exe"},
                     {"desktop_id":"id","program":"C:/Windows/System32/notepad.exe","args":"bad"},
                     {"desktop_id":"id","program":"C:/Windows/System32/notepad.exe","cwd":42}):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    self.agent.gui_desktop_launch(args)
        self.agent._isolated_desktops.launch.assert_not_called()

    def test_action_rejects_invalid_pids_and_handles(self):
        self.agent._isolated_desktops=mock.Mock()
        for key in ("hwnd","expected_pid"):
            args={"desktop_id":"id","hwnd":40,"expected_pid":30,"method":"click_button"}
            args[key]=-1
            with self.subTest(key=key),self.assertRaises(ValueError):
                self.agent.gui_desktop_action(args)
        self.agent._isolated_desktops.action.assert_not_called()

    def test_sessions_and_window_listing_dispatch(self):
        manager=mock.Mock()
        manager.sessions.return_value={"sessions":[]}
        manager.windows.return_value={"windows":[]}
        self.agent._isolated_desktops=manager
        self.assertEqual(self.agent.gui_desktop_sessions({}),{"sessions":[]})
        self.assertEqual(self.agent.gui_desktop_windows({
            "desktop_id":"test","limit":15,"with_children":False}),{"windows":[]})
        manager.windows.assert_called_once_with("test",False,15)
        with self.assertRaises(ValueError):
            self.agent.gui_desktop_windows({"desktop_id":"test","with_children":"false"})

    def test_capture_and_cleanup_dispatch(self):
        manager=mock.Mock()
        manager.capture.return_value={"png_bytes":42}
        manager.close.return_value={"desktop_handle_closed":True}
        self.agent._isolated_desktops=manager
        self.assertEqual(self.agent.gui_desktop_capture({
            "desktop_id":"abc","hwnd":23,"expected_pid":44,"max_width":600}),
            {"png_bytes":42})
        manager.capture.assert_called_once_with("abc",23,44,600)
        self.assertTrue(self.agent.gui_desktop_close({"desktop_id":"abc"})["desktop_handle_closed"])
        manager.close.assert_called_once_with("abc")

    def test_hub_catalog_includes_every_desktop_tool(self):
        manifest=json.loads((ROOT/"server/tools_manifest.json").read_text(encoding="utf8"))
        found={item["name"]:item for item in manifest}
        for name in NAMES:
            with self.subTest(name=name):
                self.assertIn(name,found)
                self.assertIn("device",found[name]["inputSchema"]["required"])
                self.assertFalse(found[name]["inputSchema"]["additionalProperties"])
                self.assertEqual(
                    found[name]["annotations"]["readOnlyHint"],
                    self.agent.tools[name][0]["annotations"]["readOnlyHint"])
        self.assertEqual(len(manifest),len(found))

    def test_no_unverified_backdoor_to_user_desktop(self):
        script=(ROOT/"client/asc_isolated_desktop.py").read_text(encoding="utf8")
        for forbidden in ("SwitchDesktopW", "SendInput", "pyautogui", "mouse_event("):
            # Mentioning forbidden functions in comments is permitted, but
            # our background implementation may not INVOKE them.
            self.assertNotIn("."+forbidden+"(",script)


if __name__=="__main__":
    unittest.main()
