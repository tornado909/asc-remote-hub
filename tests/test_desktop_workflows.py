"""Extended native desktop workflows: stdin secrecy, waits, sessions, batch and windows."""
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
from asc_desktop_native import NativeDesktop,NativeDesktopError
from asc_desktop_workflows import session_scope

class WorkflowTests(unittest.TestCase):
    def setUp(self):
        td=tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root=Path(td.name)
        self.exe=self.root/"agent-desktop.exe"
        self.exe.write_bytes(b"verified-native-test")
        for patch in (
            mock.patch("asc_desktop_native.installed",return_value=True),
            mock.patch("asc_desktop_native.native_executable",return_value=self.exe)):
            patch.start()
            self.addCleanup(patch.stop)
        self.engine=NativeDesktop(self.root)

    def reply(self,data=None,code=0):
        return types.SimpleNamespace(
            stdout=json.dumps(data or {"ok":True,"data":{"snapshot_id":"sabc123"}}).encode(),
            stderr=b"secret diagnostic never printed",returncode=code)

    def test_secret_set_value_uses_stdin_only(self):
        text="SuperPrivatePassword42"
        with mock.patch("asc_desktop_native.subprocess.run",return_value=self.reply()) as process:
            self.engine.act({"ref":"@sabc123:e1","action":"set-value","value":text})
        argv=process.call_args.args[0]
        self.assertIn("--stdin",argv)
        self.assertNotIn(text,argv)
        self.assertEqual(process.call_args.kwargs["input"],text.encode())
        self.assertNotIn("stdin",process.call_args.kwargs)

    def test_type_unicode_stdin_and_empty_rejected(self):
        value="Привет Мир! A🍂"
        with mock.patch("asc_desktop_native.subprocess.run",return_value=self.reply()) as process:
            self.engine.act({"ref":"@sabc123:e1","action":"type","value":value})
        self.assertEqual(process.call_args.kwargs["input"],value.encode("utf-8"))
        self.assertNotIn(value,process.call_args.args[0])
        with self.assertRaises(ValueError):
            self.engine.act({"ref":"@sabc123:e1","action":"type","value":""})

    def test_session_scope_is_in_environment_not_cli_arguments(self):
        with mock.patch.dict(os.environ,{"AGENT_DESKTOP_SESSION":"WRONG","AGENT_DESKTOP_TRACE":"leak.jsonl"}),\
             mock.patch("asc_desktop_native.subprocess.run",return_value=self.reply()) as process:
            self.engine.snapshot({"window_id":"w-123","session_id":"valid_A-1"})
        command=process.call_args.args[0]
        env=process.call_args.kwargs["env"]
        self.assertNotIn("--session",command)
        self.assertEqual(env["AGENT_DESKTOP_SESSION"],"valid_A-1")
        self.assertNotIn("AGENT_DESKTOP_TRACE",env)
        with self.assertRaises(ValueError):
            self.engine.snapshot({"session_id":"../leak"})

    def test_without_session_inherited_session_is_not_reused(self):
        with mock.patch.dict(os.environ,{"AGENT_DESKTOP_SESSION":"OLD_SESSION"}),\
             mock.patch("asc_desktop_native.subprocess.run",return_value=self.reply()) as process:
            self.engine.status({})
        self.assertNotIn("AGENT_DESKTOP_SESSION",process.call_args.kwargs["env"])

    def test_app_operation_restricts_launch_and_close(self):
        with mock.patch("asc_desktop_native.subprocess.run",return_value=self.reply()) as process:
            self.engine.apps({"operation":"list","app":"Editor"})
            self.assertEqual(process.call_args.args[0][1],"list-apps")
            self.engine.apps({"operation":"launch","app":"Editor"})
            self.assertEqual(process.call_args.args[0][1:3],["launch","Editor"])
            self.engine.apps({"operation":"close","app":"Editor"})
            self.assertEqual(process.call_args.args[0][1:3],["close-app","Editor"])
            self.assertNotIn("--force",process.call_args.args[0])
        with self.assertRaises(ValueError):
            self.engine.apps({"operation":"launch"})

    def test_window_management_requires_exact_target(self):
        with mock.patch("asc_desktop_native.subprocess.run",return_value=self.reply()) as process:
            self.engine.window({"operation":"move","window_id":"w-123","x":-200,"y":100})
            self.assertIn("--x=-200",process.call_args.args[0])
            self.engine.window({"operation":"resize","window_id":"w-123","width":720,"height":420})
            self.assertIn("--width",process.call_args.args[0])
        for options in ({"operation":"resize","width":700,"height":420},
                        {"operation":"move","window_id":"w-123","x":2},
                        {"operation":"focus","window_id":"invalid"},
                        {"operation":"close","app":"Editor"}):
            with self.subTest(options=options),self.assertRaises(ValueError):
                self.engine.window(options)

    def test_wait_modes_are_native_not_sleep(self):
        for mode,args,flag in (
            ("element",{"ref":"@sabc123:e5","predicate":"actionable"},"--element"),
            ("window",{"window":"Save As"},"--window"),
            ("menu",{"app":"Editor"},"--menu"),
            ("menu-closed",{"app":"Editor"},"--menu-closed"),
            ("event",{"event":"window-opened","app":"Editor"},"--event"),
            ("notification",{"app":"Editor"},"--notification"),
        ):
            with self.subTest(mode=mode),\
                 mock.patch("asc_desktop_native.subprocess.run",return_value=self.reply()) as process:
                self.engine.wait({"mode":mode,"timeout_ms":3500,**args})
                self.assertEqual(process.call_args.args[0][1],"wait")
                self.assertIn(flag,process.call_args.args[0])
                self.assertIn("--timeout",process.call_args.args[0])

    def test_post_action_wait_and_disposition(self):
        data={"ok":True,"data":{"disposition":{"delivery":"delivered_unverified","retry":"unsafe"}}}
        with mock.patch("asc_desktop_native.subprocess.run",return_value=self.reply(data)) as process:
            result=self.engine.act({"ref":"@sabc123:e1","action":"click",
                                    "wait_for":"button:Done","wait_timeout_ms":2000})
        self.assertIn("--wait-for",process.call_args.args[0])
        self.assertEqual(result["data"]["disposition"]["retry"],"unsafe")

    def test_native_failure_exposes_retry_not_sensitive_UI_details(self):
        failure={"ok":False,"error":{
            "code":"STALE_REF","message":"Secret UI text",
            "disposition":{"delivery":"not_delivered","retry":"safe"},
            "recovery":{"strategy":"refresh_snapshot","requires_fresh_snapshot":True},
            "details":{"window_name":"Private document"}}}
        with mock.patch("asc_desktop_native.subprocess.run",return_value=self.reply(failure,1)):
            with self.assertRaises(NativeDesktopError) as ctx:
                self.engine.act({"ref":"@sabc123:e1","action":"click"})
        error=ctx.exception
        self.assertEqual(error.code,"STALE_REF")
        self.assertEqual(error.disposition["retry"],"safe")
        self.assertNotIn("Private",str(error))
        self.assertNotIn("Secret",str(error))

    def test_timeout_is_uncertain_for_actions_and_safe_for_reads(self):
        import subprocess
        with mock.patch("asc_desktop_native.subprocess.run",
                        side_effect=subprocess.TimeoutExpired(cmd="mock",timeout=1)):
            with self.assertRaises(NativeDesktopError) as action:
                self.engine.act({"ref":"@sabc123:e1","action":"click"})
            self.assertEqual(action.exception.disposition["retry"],"unknown")
            with self.assertRaises(NativeDesktopError) as read:
                self.engine.windows({})
            self.assertEqual(read.exception.disposition["retry"],"safe")

    def test_session_start_does_not_trace_or_save_screenshots(self):
        with mock.patch("asc_desktop_native.subprocess.run",return_value=self.reply()) as process:
            self.engine.session({"operation":"start","name":"ui-test"})
            argv=process.call_args.args[0]
            self.assertEqual(argv[1:3],["session","start"])
            self.assertIn("--no-trace",argv)
            self.assertNotIn("--screenshots",argv)
            self.engine.session({"operation":"end","session_id":"run-123"})
            self.assertEqual(process.call_args.args[0][-1],"run-123")
            self.engine.session({"operation":"gc"})
            self.assertIn("--ended",process.call_args.args[0])

    def test_surface_snapshot_find_and_listing(self):
        with mock.patch("asc_desktop_native.subprocess.run",return_value=self.reply()) as process:
            self.engine.snapshot({"surface":"menu","app":"Editor"})
            self.assertIn("menu",process.call_args.args[0])
            self.engine.find({"surface":"alert","app":"Editor","role":"button"})
            self.assertIn("alert",process.call_args.args[0])
            self.engine.surfaces({"app":"Editor"})
            self.assertEqual(process.call_args.args[0][1],"list-surfaces")
        with self.assertRaises(ValueError):
            self.engine.snapshot({"root":"@sabc:e1","surface":"menu"})

    def test_secret_verify_does_not_return_field_value(self):
        outputs=[
            {"ok":True,"data":{"property":"value","value":"not yet"}},
            {"ok":True,"data":{"property":"value","value":"secret"}},
        ]
        with mock.patch.object(self.engine,"_run",side_effect=outputs) as native:
            result=self.engine.verify({"ref":"@sabc:e1","value":"secret","timeout_ms":5000})
        self.assertTrue(result["matched"])
        self.assertNotIn("secret",str(result))
        self.assertEqual(native.call_count,2)
        self.assertFalse(any("secret" in str(x) for x in native.call_args_list))

    def test_batch_find_then_act_uses_ref_and_idle_policy(self):
        native=mock.Mock()
        find_result={"ok":True,"data":{"match":{"ref_id":"@sabc:e4"}}}
        with mock.patch.object(self.engine,"find",return_value=find_result),\
             mock.patch.object(self.engine,"act",return_value={"ok":True,"data":{}}) as act:
            batch=self.engine.batch({"steps":[
                {"op":"find","args":{"role":"button","name":"Save","app":"Editor","first":True}},
                {"op":"act","args":{"ref_from":0,"action":"click"}},
            ]},native)
        self.assertTrue(batch["ok"])
        native.assert_called_once()
        self.assertEqual(act.call_args.args[0]["ref"],"@sabc:e4")
        self.assertFalse(batch["atomic"])

    def test_batch_rejects_bad_steps_before_any_action(self):
        native=mock.Mock()
        with self.assertRaises(ValueError):
            self.engine.batch({"steps":[
                {"op":"act","args":{"ref":"@sabc:e1","action":"click"}},
                {"op":"shell","args":{"command":"delete all"}}
            ]},native)
        native.assert_not_called()

    def test_batch_unsafe_failure_halts_even_if_stop_on_error_false(self):
        native=mock.Mock()
        err=NativeDesktopError("TIMEOUT",{"delivery":"unknown","retry":"unknown"})
        with mock.patch.object(self.engine,"act",side_effect=err) as act:
            result=self.engine.batch({"stop_on_error":False,"steps":[
                {"op":"act","args":{"ref":"@sabc:e1","action":"click"}},
                {"op":"act","args":{"ref":"@sabc:e2","action":"click"}}
            ]},native)
        self.assertFalse(result["ok"])
        self.assertEqual(result["steps_completed"],1)
        self.assertEqual(result["results"][0]["disposition"]["retry"],"unknown")
        act.assert_called_once()

    def test_batch_verify_uses_in_memory_comparison_and_ref_dependency(self):
        result_find={"ok":True,"data":{"match":{"ref_id":"@sabc:e4"}}}
        with mock.patch.object(self.engine,"find",return_value=result_find),\
             mock.patch.object(self.engine,"verify",return_value={"ok":True,"matched":True}) as check:
            result=self.engine.batch({"steps":[
                {"op":"find","args":{"role":"textfield","name":"Name","app":"Editor"}},
                {"op":"verify","args":{"ref_from":0,"value":"secret","timeout_ms":2000}}
            ]},lambda:None)
        self.assertTrue(result["ok"])
        self.assertEqual(check.call_args.args[0]["ref"],"@sabc:e4")
        self.assertEqual(check.call_args.args[0]["value"],"secret")

    def test_session_snapshot_pruning_cannot_touch_another_session(self):
        sessions=self.engine.state/"sessions"
        own=sessions/"one"/"snapshots"
        other=sessions/"other"/"snapshots"
        own.mkdir(parents=True)
        other.mkdir(parents=True)
        for i in range(38):(own/f"s{i:04d}").mkdir()
        (other/"s0000").mkdir()
        self.engine._prune_snapshots("one")
        self.assertEqual(len(list(own.iterdir())),32)
        self.assertEqual(len(list(other.iterdir())),1)
        with self.assertRaises(ValueError):
            self.engine._prune_snapshots("../other")

    def test_batch_large_snapshots_are_summarized_and_deadline_cleared(self):
        tree={"tree":"x"*140_000,"snapshot_id":"stest123","ref_count":123}
        with mock.patch.object(self.engine,"snapshot",return_value={"ok":True,"data":tree}):
            result=self.engine.batch({"steps":[{"op":"snapshot","args":{"max_depth":3}}]},lambda:None)
        self.assertTrue(result["ok"])
        compact=result["results"][0]["result"]
        self.assertTrue(compact["truncated_for_batch"])
        self.assertNotIn("x"*1000,str(compact))
        self.assertEqual(compact["data"]["ref_count"],123)
        self.assertIsNone(getattr(self.engine,"_batch_deadline",None))

    def test_batch_deadline_limits_native_process(self):
        import time
        self.engine._batch_deadline=time.monotonic()+0.5
        try:
            with mock.patch("asc_desktop_native.subprocess.run",
                            return_value=self.reply()) as process:
                self.engine.status({})
            self.assertLess(process.call_args.kwargs["timeout"],1.0)
        finally:
            self.engine._batch_deadline=None

    def test_public_manifest_matches_semantic_agent(self):
        x=json.loads((ROOT/"server/tools_manifest.json").read_text())
        names={v["name"] for v in x}
        required={"desktop_wait","desktop_verify","desktop_session","desktop_apps",
                  "desktop_window","desktop_batch","desktop_surfaces"}
        self.assertTrue(required<=names)
        for v in x:
            if v["name"] in required:
                self.assertIn("device",v["inputSchema"]["required"])
                self.assertFalse(v["inputSchema"]["additionalProperties"])


if __name__=="__main__":
    unittest.main()
