"""Bounded screenshot->action grounding; never uses a real desktop or mouse."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"client"))
from asc_remote_commander import Agent, AccessDenied, normalized_window_point


class WindowGroundingTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        env=mock.patch.dict(os.environ,{
            "ASC_RC_ROOTS":temporary.name,
            "ASC_RC_STATE_DIR":str(Path(temporary.name)/"state"),
            "ASC_RC_ALLOW_GUI":"1",
            "ASC_RC_ALLOW_EXEC":"0",
            "ASC_RC_ALLOW_WRITE":"0",
            "ASC_RC_ALLOW_DELETE":"0",
            "ASC_RC_ALLOW_INTERACTIVE_GUI":"0",
        })
        env.start()
        self.addCleanup(env.stop)
        self.agent=Agent()
        self.window={
            "hwnd":41234,"pid":5512,"title":"Sample Window Editor",
            "visible":True,"foreground":True,
            "rect":{"left":-1200,"top":-300,"right":-400,"bottom":300}}
        self.args={
            "hwnd":41234,"expected_pid":5512,
            "title_contains":"Sample Window",
            "allow_desktop_interference":True,
            "x":500,"y":500,
        }

    def test_window_mapping_handles_negative_monitor_offsets_and_edges(self):
        rect=self.window["rect"]
        self.assertEqual(normalized_window_point(rect,0,0),(-1200,-300))
        self.assertEqual(normalized_window_point(rect,1000,1000),(-401,299))
        self.assertEqual(normalized_window_point(rect,500,500),(-800,0))

    def test_rejects_invalid_coordinates_and_rectangles(self):
        rect=self.window["rect"]
        for x,y in [(-1,500),(500,1001),(True,500),("500",500),(0.5,500)]:
            with self.subTest(x=x,y=y),self.assertRaises(ValueError):
                normalized_window_point(rect,x,y)
        for bad in (
            {"left":0,"top":0,"right":0,"bottom":100},
            {"left":0,"top":0,"right":100,"bottom":True},
            {"left":0,"top":0,"right":100},
        ):
            with self.subTest(rect=bad),self.assertRaises(ValueError):
                normalized_window_point(bad,500,500)

    def test_capture_only_records_geometry_and_is_bounded(self):
        tokens=[self.agent._remember_window_capture(self.window) for _ in range(21)]
        self.assertEqual(len(self.agent._window_captures),16)
        self.assertEqual(len(set(tokens)),21)
        entry=self.agent._window_captures[tokens[-1]]
        self.assertEqual(set(entry),{"when","hwnd","pid","rect"})
        self.assertNotIn("image",entry)
        self.assertNotIn("pixels",entry)
        self.assertEqual(entry["rect"],self.window["rect"])
        entry["rect"]["left"]=0
        self.assertEqual(self.window["rect"]["left"],-1200)

    def test_denies_click_by_default_and_does_not_consume_capture(self):
        capture=self.agent._remember_window_capture(self.window)
        with mock.patch.object(self.agent,"_gui") as gui:
            with self.assertRaises(AccessDenied):
                self.agent.click_window_normalized({
                    **self.args,"capture_id":capture})
            gui.assert_not_called()
        self.assertIn(capture,self.agent._window_captures)

    def test_normalized_click_uses_verified_window_rect_only_once(self):
        self.agent.allow_interactive_gui=True
        capture=self.agent._remember_window_capture(self.window)
        with mock.patch.object(self.agent,"_window_details",return_value=self.window),\
             mock.patch.object(self.agent,"_gui") as gui:
            result=self.agent.click_window_normalized({
                **self.args,"capture_id":capture,"button":"left","clicks":2})
            self.assertEqual(result["clicked"],[-800,0])
            self.assertEqual(result["normalized"],[500,500])
            self.assertTrue(result["requires_new_screenshot"])
            gui.return_value.click.assert_called_once_with(-800,0,clicks=2,button="left")
            self.assertNotIn(capture,self.agent._window_captures)
            with self.assertRaises(AccessDenied):
                self.agent.click_window_normalized({**self.args,"capture_id":capture})
            gui.return_value.click.assert_called_once()

    def test_window_geometry_move_refuses_click(self):
        self.agent.allow_interactive_gui=True
        capture=self.agent._remember_window_capture(self.window)
        moved={**self.window,"rect":{**self.window["rect"],"left":-1199}}
        with mock.patch.object(self.agent,"_window_details",return_value=moved),\
             mock.patch.object(self.agent,"_gui") as gui:
            with self.assertRaisesRegex(AccessDenied,"moved or resized"):
                self.agent.click_window_normalized({**self.args,"capture_id":capture})
            gui.assert_not_called()

    def test_capture_from_different_window_identity_refuses_click(self):
        self.agent.allow_interactive_gui=True
        capture=self.agent._remember_window_capture(self.window)
        different={**self.window,"pid":5513}
        with mock.patch.object(self.agent,"_window_details",return_value=different),\
             mock.patch.object(self.agent,"_gui") as gui:
            with self.assertRaises(ValueError):
                self.agent.click_window_normalized({**self.args,"capture_id":capture})
            gui.assert_not_called()
        self.assertIn(capture,self.agent._window_captures)

    def test_capture_expired_or_replayed_fails_closed(self):
        self.agent.allow_interactive_gui=True
        capture=self.agent._remember_window_capture(self.window)
        self.agent._window_captures[capture]["when"]=time.monotonic()-31
        with mock.patch.object(self.agent,"_window_details",return_value=self.window),\
             mock.patch.object(self.agent,"_gui") as gui:
            with self.assertRaisesRegex(AccessDenied,"expired"):
                self.agent.click_window_normalized({**self.args,"capture_id":capture})
            gui.assert_not_called()

    def test_foreground_race_refuses_injection(self):
        self.agent.allow_interactive_gui=True
        capture=self.agent._remember_window_capture(self.window)
        background={**self.window,"foreground":False}
        with mock.patch.object(self.agent,"_window_details",side_effect=[self.window,background]),\
             mock.patch.object(self.agent,"_gui") as gui:
            with self.assertRaises(AccessDenied):
                self.agent.click_window_normalized({**self.args,"capture_id":capture})
            gui.return_value.click.assert_not_called()

    def test_screenshot_returns_capture_id_and_correct_source_geometry(self):
        class Image:
            width=800
            height=600
            def thumbnail(self,bounds):
                raise AssertionError("not needed")
            def save(self,stream,**kwargs):
                stream.write(b"mock-png")
        mocked_pil=types.ModuleType("PIL")
        mocked_pil.ImageGrab=types.SimpleNamespace(grab=mock.Mock(return_value=Image()))
        with mock.patch.dict(sys.modules,{"PIL":mocked_pil}),\
             mock.patch.object(self.agent,"_window_details",return_value=self.window):
            response=self.agent.window_screenshot({"hwnd":41234,"max_width":1600})
        self.assertIn("capture_id",response)
        self.assertEqual(response["capture_ttl_seconds"],30)
        self.assertEqual(response["window_rect"],self.window["rect"])
        self.assertEqual((response["source_width"],response["source_height"]),(800,600))
        self.assertEqual((response["width"],response["height"]),(800,600))
        self.assertIn(response["capture_id"],self.agent._window_captures)

    def test_windows_registration_matches_public_manifest(self):
        # On Linux, temporarily exercise the Windows-only registration path,
        # but never instantiate a WindowsPath or use real OS input.
        with mock.patch("asc_remote_commander.os.name","nt"):
            self.agent.tools.clear()
            self.agent._register_tools()
        self.assertIn("click_window_normalized",self.agent.tools)
        registered=self.agent.tools["click_window_normalized"][0]
        manifest=json.loads((ROOT/"server/tools_manifest.json").read_text(encoding="utf8"))
        public=next(x for x in manifest if x["name"]=="click_window_normalized")
        self.assertEqual(set(registered["inputSchema"]["required"])|{"device"},
                         set(public["inputSchema"]["required"]))
        self.assertFalse(public["annotations"]["readOnlyHint"])

    def test_tool_is_registered_with_consent_and_gateway_serialization(self):
        manifest=json.loads((ROOT/"server/tools_manifest.json").read_text(encoding="utf8"))
        item=next(x for x in manifest if x["name"]=="click_window_normalized")
        schema=item["inputSchema"]
        self.assertFalse(item["annotations"]["readOnlyHint"])
        self.assertFalse(schema["additionalProperties"])
        for name in ("device","hwnd","expected_pid","title_contains",
                     "allow_desktop_interference","capture_id","x","y"):
            self.assertIn(name,schema["required"])
        for field in ("x","y"):
            self.assertEqual(schema["properties"][field]["minimum"],0)
            self.assertEqual(schema["properties"][field]["maximum"],1000)
        from bridge import GUI_JOBS
        self.assertIn("click_window_normalized",GUI_JOBS)
        self.assertNotIn("click_window_normalized",self.agent.tools,
                         "Non-Windows agents must not advertise Windows input")


if __name__=="__main__":
    unittest.main()
