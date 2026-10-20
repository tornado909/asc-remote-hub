"""Fail-closed Windows GUI safety tests. No physical mouse/keyboard actions."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"client"))
from asc_remote_commander import AccessDenied,Agent


class SafeGuiTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env=mock.patch.dict(os.environ,{
            "ASC_RC_ROOTS":tmp.name,
            "ASC_RC_STATE_DIR":str(Path(tmp.name)/"state"),
            "ASC_RC_ALLOW_GUI":"1",
            "ASC_RC_ALLOW_EXEC":"1",
            "ASC_RC_ALLOW_WRITE":"0",
            "ASC_RC_ALLOW_DELETE":"0"
        })
        env.start()
        self.addCleanup(env.stop)
        self.agent=Agent()
        self.window={"hwnd":48123,"pid":4999,"title":"Demo Window - Editor",
                     "visible":True,"foreground":True,
                     "rect":{"left":100,"top":100,"right":500,"bottom":400}}
        self.identity={"hwnd":48123,"expected_pid":4999,
                       "title_contains":"Demo Window"}

    def test_shared_desktop_tools_fail_closed_without_explicit_opt_in(self):
        self.agent.allow_interactive_gui=True
        cases=[
            ("click_mouse",{"x":200,"y":200}),
            ("type_text",{"text":"hello"}),
            ("press_keys",{"keys":["ctrl","s"]}),
            ("scroll_mouse",{"clicks":1,"direction":"down"}),
            ("terminal_send",{"text":"ping","submit":True}),
            ("window_focus",{})
        ]
        with mock.patch.object(self.agent,"_gui") as desktop, \
             mock.patch.object(self.agent,"_windows") as native, \
             mock.patch.object(self.agent,"_window_details") as details:
            for name,args in cases:
                with self.subTest(name=name):
                    with self.assertRaises(AccessDenied):
                        getattr(self.agent,name)({**self.identity,**args})
            desktop.assert_not_called()
            native.assert_not_called()
            details.assert_not_called()

    def test_identity_and_foreground_guards_reject_cross_window_input(self):
        self.agent.allow_interactive_gui=True
        a={**self.identity,"allow_desktop_interference":True,"x":200,"y":200}
        with mock.patch.object(self.agent,"_window_details",return_value={
                **self.window,"foreground":False}),mock.patch.object(self.agent,"_gui") as gui:
            with self.assertRaises(AccessDenied):
                self.agent.click_mouse(a)
            gui.assert_not_called()
        with mock.patch.object(self.agent,"_window_details",return_value={
                **self.window,"pid":5001}),mock.patch.object(self.agent,"_gui") as gui:
            with self.assertRaises(ValueError):
                self.agent.click_mouse(a)
            gui.assert_not_called()
        with mock.patch.object(self.agent,"_window_details",return_value={
                **self.window,"title":"User Messenger"}),mock.patch.object(self.agent,"_gui") as gui:
            with self.assertRaises(ValueError):
                self.agent.click_mouse(a)
            gui.assert_not_called()

    def test_click_must_be_inside_verified_target_even_with_opt_in(self):
        self.agent.allow_interactive_gui=True
        a={**self.identity,"allow_desktop_interference":True,
           "x":510,"y":230}
        with mock.patch.object(self.agent,"_window_details",return_value=self.window), \
             mock.patch.object(self.agent,"_gui") as gui:
            with self.assertRaises(AccessDenied):
                self.agent.click_mouse(a)
            gui.assert_not_called()

    def test_background_inspection_only_calls_uia_no_desktop(self):
        with mock.patch.object(self.agent,"_window_details",return_value=self.window), \
             mock.patch.object(self.agent,"_windows_uia",return_value={"ok":True,"controls":[]}) as uia, \
             mock.patch.object(self.agent,"_gui") as gui:
            result=self.agent.window_controls({**self.identity,"depth":5,"limit":50})
            self.assertEqual(result["controls"],[])
            uia.assert_called_once_with({
                "mode":"list","hwnd":48123,"expected_pid":4999,
                "title_contains":"Demo Window","limit":50,"depth":5})
            gui.assert_not_called()

    def test_background_action_rejects_ambiguous_selector_early(self):
        with mock.patch.object(self.agent,"_window_details",return_value=self.window), \
             mock.patch.object(self.agent,"_windows_uia") as uia, \
             mock.patch.object(self.agent,"_gui") as gui:
            for bad in ({"control_type":"Button"},{"name":""},{}):
                with self.subTest(selector=bad):
                    with self.assertRaises(ValueError):
                        self.agent.window_action({**self.identity,"action":"invoke","selector":bad})
            with self.assertRaises(ValueError):
                self.agent.window_action({
                    **self.identity,"action":"invoke",
                    "selector":{"automation_id":"ID:save"},
                    "value":"unwanted"})
            uia.assert_not_called()
            gui.assert_not_called()

    def test_background_action_sends_only_targeted_uia_payload(self):
        with mock.patch.object(self.agent,"_window_details",return_value=self.window), \
             mock.patch.object(self.agent,"_windows_uia",return_value={
                 "ok":True,"mode":"background","action":"set_value",
                 "cursor_unchanged":True,"foreground_unchanged":True,
                 "value_verified":True}) as uia, \
             mock.patch.object(self.agent,"_require_desktop_idle",return_value=45), \
             mock.patch.object(self.agent,"_gui") as gui:
            result=self.agent.window_action({
                **self.identity,"action":"set_value",
                "selector":{"automation_id":"editor-input","control_type":"Edit"},
                "value":"Привет"})
            self.assertTrue(result["cursor_unchanged"])
            self.assertTrue(result["value_verified"])
            self.assertEqual(uia.call_args.args[0]["value"],"Привет")
            self.assertEqual(uia.call_args.args[0]["expected_pid"],4999)
            self.assertEqual(uia.call_args.args[0]["min_idle_seconds"],45)
            gui.assert_not_called()

    def test_no_fallback_on_uia_error(self):
        with mock.patch.object(self.agent,"_window_details",return_value=self.window), \
             mock.patch.object(self.agent,"_windows_uia",side_effect=RuntimeError("UIA_CONTROL_PATTERN_UNSUPPORTED")), \
             mock.patch.object(self.agent,"_require_desktop_idle",return_value=45), \
             mock.patch.object(self.agent,"_gui") as gui:
            with self.assertRaisesRegex(RuntimeError,"UNSUPPORTED"):
                self.agent.window_action({
                    **self.identity,
                    "selector":{"automation_id":"unknown"},
                    "action":"invoke"})
            gui.assert_not_called()

    def test_foreground_switch_is_a_separately_explicit_action(self):
        self.agent.allow_interactive_gui=True
        # Even with GUI privilege, window_focus does not Alt-spoof or trigger
        # any desktop action without a per-call permit.
        with mock.patch.object(self.agent,"_windows") as native:
            with self.assertRaises(AccessDenied):
                self.agent.window_focus(self.identity)
            native.assert_not_called()

    def test_local_policy_refuses_even_explicit_interactive_opt_in(self):
        self.assertFalse(self.agent.allow_interactive_gui)
        with mock.patch.object(self.agent,"_window_details",return_value=self.window), \
             mock.patch.object(self.agent,"_gui") as gui:
            with self.assertRaises(AccessDenied):
                self.agent.click_mouse({**self.identity,
                    "allow_desktop_interference":True,"x":190,"y":180})
            gui.assert_not_called()

    def test_active_user_blocks_uia_mutation_even_with_valid_selector(self):
        with mock.patch.object(self.agent,"_window_details",return_value=self.window), \
             mock.patch.object(self.agent,"_desktop_idle_seconds",return_value=1.8), \
             mock.patch.object(self.agent,"_windows_uia") as backend:
            with self.assertRaisesRegex(AccessDenied,"UserActive"):
                self.agent.window_action({**self.identity,
                    "selector":{"automation_id":"safe-test-button"},
                    "action":"invoke"})
            backend.assert_not_called()

    def test_focusing_uia_provider_is_blocked_after_first_detection(self):
        with mock.patch.object(self.agent,"_window_details",return_value=self.window), \
             mock.patch.object(self.agent,"_require_desktop_idle",return_value=45), \
             mock.patch.object(self.agent,"_windows_uia",return_value={
                 "ok":True,"foreground_unchanged":False}) as provider:
            with self.assertRaisesRegex(RuntimeError,"UIA_FOCUS_INTERFERENCE"):
                self.agent.window_action({**self.identity,
                    "selector":{"automation_id":"test-button"},"action":"invoke"})
            with self.assertRaisesRegex(AccessDenied,"previously stole focus"):
                self.agent.window_action({**self.identity,
                    "selector":{"automation_id":"test-button"},"action":"invoke"})
            provider.assert_called_once()

    def test_schema_registration_and_capabilities(self):
        # The script/manifest use schemas that permit no unknown arguments.
        from pathlib import Path
        manifest=json.loads((ROOT/"server/tools_manifest.json").read_text(encoding="utf8"))
        for tool in ("window_controls","window_action","click_mouse","terminal_send"):
            entries=[x for x in manifest if x.get("name")==tool]
            self.assertEqual(len(entries),1,tool)
            schema=entries[0]["inputSchema"]
            self.assertFalse(schema["additionalProperties"])
            self.assertIn("device",schema["required"])
        for tool in ("window_action","click_mouse","type_text","press_keys","terminal_send"):
            entries=[x for x in manifest if x.get("name")==tool]
            self.assertTrue(entries and entries[0]["annotations"]["readOnlyHint"] is False)


if __name__=="__main__":
    unittest.main()
