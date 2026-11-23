"""Pinned upstream agent-desktop bridge. No desktop input in these tests."""
import hashlib
import importlib.util
import io
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
from asc_desktop_native import (
    MAX_OUTPUT_BYTES, NativeDesktop, NativeDesktopError,
    REF_PATTERN, WINDOWS_EXE_SHA256, _bounded_int, _ref
)
from asc_remote_commander import Agent, AccessDenied


class NativeDesktopTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        self.exe=self.root/"agent-desktop.exe"
        self.exe.write_bytes(b"simulated signed executable")
        self.fake_ok={"version":"2.6","ok":True,"command":"snapshot",
                      "data":{"snapshot_id":"s123xyz","ref_count":2}}
        self.patches=[
            mock.patch("asc_desktop_native.installed",return_value=True),
            mock.patch("asc_desktop_native.native_executable",return_value=self.exe)
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.native=NativeDesktop(self.root)

    def response(self,response=None,returncode=0):
        return types.SimpleNamespace(
            stdout=json.dumps(response or self.fake_ok).encode(),
            stderr=b"",returncode=returncode
        )

    def test_status_passes_no_shell_and_isolated_state_root(self):
        with mock.patch("asc_desktop_native.subprocess.run",return_value=self.response()) as subprocess_run:
            res=self.native.status({})
        self.assertTrue(res["ok"])
        kwargs=subprocess_run.call_args.kwargs
        self.assertEqual(subprocess_run.call_args.args[0],[
            str(self.exe),"status"
        ])
        self.assertEqual(kwargs["env"]["AGENT_DESKTOP_HOME"],str(self.native.state))
        self.assertEqual(kwargs["stdin"],__import__("subprocess").DEVNULL)
        self.assertFalse(kwargs["check"])
        self.assertEqual(kwargs["creationflags"],0)

    def test_snapshot_is_semantic_interactive_compact_and_not_screenshot(self):
        with mock.patch("asc_desktop_native.subprocess.run",return_value=self.response()) as run:
            self.native.snapshot({"window_id":"w-1234","max_depth":6})
        argv=run.call_args.args[0]
        self.assertEqual(argv[1],"snapshot")
        self.assertIn("--interactive-only",argv)
        self.assertIn("--compact",argv)
        self.assertIn("--window-id",argv)
        self.assertNotIn("--screenshot",argv)
        self.assertNotIn("--headed",argv)

    def test_snapshot_supports_skeleton_and_drill(self):
        with mock.patch("asc_desktop_native.subprocess.run",return_value=self.response()) as run:
            self.native.snapshot({"window_id":"w-123","skeleton":True})
            self.assertIn("--skeleton",run.call_args.args[0])
            self.native.snapshot({"root":"@sabc123:e2","max_depth":9})
            self.assertIn("--root",run.call_args.args[0])
        with self.assertRaises(ValueError):
            self.native.snapshot({"root":"@sabc123:e2","skeleton":True})

    def test_scope_ambiguity_rejected(self):
        cases=[
            {"window_id":"w-12","app":"Editor"},
            {"root":"@sabc:e1","window_id":"w-1"},
            {"window_id":"../bin"},
            {"root":"@e1"},
        ]
        for opts in cases:
            with self.subTest(opts=opts),self.assertRaises(ValueError):
                self.native.snapshot(opts)

    def test_find_uses_exact_accessible_roles_without_fuzzy_screen_read(self):
        with mock.patch("asc_desktop_native.subprocess.run",return_value=self.response()) as run:
            self.native.find({"role":"button","name":"Save","app":"Editor"})
        args=run.call_args.args[0]
        self.assertIn("--role",args)
        self.assertIn("--name",args)
        self.assertIn("--exact",args)
        self.assertNotIn("--headed",args)
        with self.assertRaises(ValueError):
            self.native.find({})

    def test_ref_validation_rejects_pixel_or_bare_ids(self):
        for ref in ("@e2","@abc:e2","@sabc:e0","42,45",
                    "@sabc:e2 --headed",None):
            with self.subTest(ref=ref),self.assertRaises(ValueError):
                _ref(ref)
        self.assertEqual(_ref("@sabc123:e4"),"@sabc123:e4")

    def test_headless_actions_require_explicit_semantic_verb(self):
        with mock.patch("asc_desktop_native.subprocess.run",return_value=self.response()) as run:
            self.native.act({"ref":"@sabc123:e4","action":"set-value","value":"Привет"})
        args=run.call_args.args[0]
        self.assertEqual(args[1:4],["set-value","@sabc123:e4","--stdin"])
        self.assertNotIn("Привет",args)
        self.assertEqual(run.call_args.kwargs["input"],"Привет".encode("utf-8"))
        self.assertNotIn("stdin",run.call_args.kwargs)
        for opts in (
            {"ref":"@sabc123:e4","action":"mouse-click"},
            {"ref":"@sabc123:e4","action":"focus"},
            {"ref":"@sabc123:e4","action":"set-value","value":""},
            {"ref":"@sabc123:e4","action":"click","value":"text"},
            {"ref":"@sabc123:e4","action":"type","value":5},
        ):
            with self.subTest(opts=opts),self.assertRaises(ValueError):
                self.native.act(opts)
        self.assertEqual(run.call_count,1)

    def test_raw_unsafe_flags_are_blocked_before_process_spawn(self):
        with mock.patch("asc_desktop_native.subprocess.run") as run:
            with self.assertRaises(ValueError):
                self.native._run("snapshot",["--headed"])
            with self.assertRaises(ValueError):
                self.native._run("snapshot",["--screenshot","leak.html"])
        run.assert_not_called()

    def test_cli_error_codes_survive_without_private_process_stderr(self):
        with mock.patch("asc_desktop_native.subprocess.run",return_value=self.response(
                {"ok":False,"error":{"code":"STALE_REF","message":"private window name"}},
                returncode=1)):
            with self.assertRaisesRegex(NativeDesktopError,"STALE_REF") as cm:
                self.native.get({"ref":"@sabc123:e7"})
            self.assertNotIn("private",str(cm.exception))

    def test_giant_output_refused(self):
        fake=types.SimpleNamespace(stdout=b"x"*(MAX_OUTPUT_BYTES+1),stderr=b"",returncode=0)
        with mock.patch("asc_desktop_native.subprocess.run",return_value=fake):
            with self.assertRaisesRegex(NativeDesktopError,"OUTPUT_TOO_LARGE"):
                self.native.windows({})

    def test_snapshot_retention_bounded_to_32(self):
        store=self.native.state/"snapshots"
        store.mkdir(parents=True)
        for i in range(36):
            (store/f"s{i:04d}").mkdir()
        self.native._prune_snapshots()
        self.assertEqual(sum(p.is_dir() for p in store.iterdir()),32)

    def test_install_pins_both_archive_and_executable(self):
        spec=importlib.util.spec_from_file_location(
            "native_installer",ROOT/"install/setup_native_desktop.py")
        mod=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(mod.WINDOWS_EXE_SHA256,WINDOWS_EXE_SHA256)
        self.assertRegex(mod.ARCHIVE_SHA256,r"^[a-f0-9]{64}$")
        self.assertIn("vercel-labs/agent-desktop",mod.SOURCE)
        self.assertLess(mod.MAX_ARCHIVE,20_000_000)

    def test_manifest_has_typed_native_tools_and_no_arbitrary_args(self):
        catalogue=json.loads((ROOT/"server/tools_manifest.json").read_text("utf8"))
        tools={tool["name"]:tool for tool in catalogue}
        names={"desktop_status","desktop_windows","desktop_snapshot","desktop_find",
               "desktop_get","desktop_is","desktop_act"}
        self.assertTrue(names<=set(tools))
        for name in names:
            schema=tools[name]["inputSchema"]
            self.assertFalse(schema["additionalProperties"])
            self.assertIn("device",schema["required"])
            self.assertIn("remote.access",tools[name]["securitySchemes"][0]["scopes"])
        self.assertFalse(tools["desktop_act"]["annotations"]["readOnlyHint"])
        self.assertTrue(tools["desktop_snapshot"]["annotations"]["readOnlyHint"])
        from bridge import GUI_JOBS
        self.assertTrue(names<=GUI_JOBS)


class AgentNativeGateTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        p=mock.patch.dict(os.environ,{
            "ASC_RC_ROOTS":temp.name,
            "ASC_RC_STATE_DIR":str(Path(temp.name)/"state"),
            "ASC_RC_ALLOW_GUI":"1","ASC_RC_ALLOW_EXEC":"0",
            "ASC_RC_ALLOW_WRITE":"0","ASC_RC_ALLOW_DELETE":"0"
        })
        p.start()
        self.addCleanup(p.stop)
        self.agent=Agent()

    def test_without_verified_binary_tool_not_advertised(self):
        self.assertNotIn("desktop_act",self.agent.tools)
        with mock.patch("asc_remote_commander.os.name","nt"),\
             mock.patch("asc_desktop_native.installed",return_value=False):
            self.agent.tools.clear()
            self.agent._register_tools()
        self.assertNotIn("desktop_act",self.agent.tools)

    def test_verified_binary_windows_tools_registered(self):
        with mock.patch("asc_remote_commander.os.name","nt"),\
             mock.patch("asc_desktop_native.installed",return_value=True):
            self.agent.tools.clear()
            self.agent._register_tools()
        for name in ("desktop_status","desktop_windows","desktop_snapshot",
                     "desktop_find","desktop_get","desktop_is","desktop_act"):
            self.assertIn(name,self.agent.tools)

    def test_native_action_preserves_idle_gate(self):
        native=mock.Mock()
        native.act.return_value={"ok":True}
        with mock.patch.object(self.agent,"_native_desktop",return_value=native),\
             mock.patch.object(self.agent,"_require_desktop_idle",
                side_effect=AccessDenied("UserActive")):
            with self.assertRaises(AccessDenied):
                self.agent.desktop_act({"ref":"@s123:e1","action":"click"})
        native.act.assert_not_called()

    def test_no_snapshot_or_physical_input_from_agent_wrapper(self):
        native=mock.Mock()
        native.snapshot.return_value={"ok":True}
        with mock.patch.object(self.agent,"_native_desktop",return_value=native),\
             mock.patch.object(self.agent,"screenshot",
                side_effect=AssertionError("No screenshots")):
            result=self.agent.desktop_snapshot({"window_id":"w-1"})
        self.assertTrue(result["ok"])


if __name__=="__main__":
    unittest.main()
