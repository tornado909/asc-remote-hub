"""ASC Remote Hub 1.1 regression tests for scheduler, latency and safe tools."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest import TestCase, main, mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"client"))
sys.path.insert(0,str(ROOT/"server"))
from asc_remote_commander import Agent,AccessDenied
from bridge import execute_agent_job,MAX_CONCURRENT_JOBS
from hub import Hub
from traffic_trace import TrafficTrace


class NewAgentToolsTests(TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name)
        self.allowed=root/"allowed"
        self.allowed.mkdir()
        self.outside=root/"outside"
        self.outside.mkdir()
        self.env=mock.patch.dict(os.environ,{
            "ASC_RC_ROOTS":str(self.allowed),
            "ASC_RC_STATE_DIR":str(root/"agent-state"),
            "ASC_RC_ALLOW_WRITE":"1",
            "ASC_RC_ALLOW_EXEC":"1",
            "ASC_RC_ALLOW_GUI":"0",
            "ASC_RC_ALLOW_DELETE":"0"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.agent=Agent()

    def test_batch_read_and_stats_preserve_root_guards(self):
        files=[]
        for n in range(6):
            f=self.allowed/("file%02d.txt"%n)
            f.write_text("line one\nline two\n",encoding="utf-8")
            files.append(str(f))
        result=self.agent.read_files({"files":[{"path":p,"limit":1} for p in files]})
        self.assertFalse(result["truncated"])
        self.assertEqual(len(result["files"]),6)
        self.assertEqual(result["files"][0]["text"],"line one\n")
        stats=self.agent.stat_paths({"paths":files})
        self.assertEqual(len(stats["entries"]),6)
        with self.assertRaises(AccessDenied):
            self.agent.read_files({"files":[{"path":files[0]},{"path":str(self.outside/"secret.txt")}]})
        with self.assertRaises(ValueError):
            self.agent.read_files({"files":[]})
        with self.assertRaises(ValueError):
            self.agent.stat_paths({"paths":files*12})

    def test_directory_tree_and_text_search_are_bounded(self):
        sub=self.allowed/"src"
        sub.mkdir()
        (sub/"important.py").write_text("a=42\nNeedle is here\n",encoding="utf-8")
        (sub/"ignored.md").write_text("needle\n",encoding="utf-8")
        hidden=self.allowed/".git"
        hidden.mkdir()
        (hidden/"private.py").write_text("needle\n",encoding="utf-8")
        (self.outside/"x.py").write_text("needle\n",encoding="utf-8")
        try:
            (sub/"outside").symlink_to(self.outside,target_is_directory=True)
        except OSError:
            pass
        results=self.agent.search_text({"path":str(self.allowed),
                            "query":"needle","pattern":"*.py","depth":3})
        self.assertEqual(len(results["matches"]),1)
        self.assertEqual(results["matches"][0]["line"],2)
        tree=self.agent.directory_tree({"path":str(self.allowed),"depth":2,"limit":50})
        self.assertTrue(any(item["path"].endswith("important.py") for item in tree["entries"]))
        self.assertFalse(any(item["path"].endswith("outside/x.py") for item in tree["entries"]))
        with self.assertRaises(AccessDenied):
            self.agent.search_text({"path":str(self.outside),"query":"needle"})
        with self.assertRaises(ValueError):
            self.agent.search_text({"path":str(self.allowed),"query":""})

    def test_terminal_send_never_submits_after_focus_change(self):
        self.agent.allow_gui=True
        fake_gui=mock.Mock()
        fake_gui.write.return_value=None
        with mock.patch.object(self.agent,"_interactive_target",
                               return_value={"hwnd":123,"pid":77}), \
             mock.patch.object(self.agent,"_gui",return_value=fake_gui), \
             mock.patch.object(self.agent,"_target_window",side_effect=[
                 {"foreground":True,"pid":77},
                 AccessDenied("Foreground changed")]):
            with self.assertRaises(AccessDenied):
                self.agent.terminal_send({"hwnd":123,"expected_pid":77,
                     "title_contains":"Codex","text":"Ping","submit":True,
                     "allow_desktop_interference":True})
        fake_gui.press.assert_not_called()
        # Missing opt-in is refused before any attempt to inspect the input.
        with self.assertRaises(AccessDenied):
            self.agent.terminal_send({"hwnd":123,"expected_pid":77,
                 "title_contains":"Codex","text":"one\ntwo",
                 "allow_desktop_interference":False})
        with mock.patch.object(self.agent,"_interactive_target",
                               return_value={"hwnd":123,"pid":77}):
            with self.assertRaises(ValueError):
                self.agent.terminal_send({"hwnd":123,"expected_pid":77,
                     "title_contains":"Codex","text":"one\ntwo",
                     "allow_desktop_interference":True})

    def test_terminal_send_success_without_focus_stealing(self):
        self.agent.allow_gui=True
        fake_gui=mock.Mock()
        with mock.patch.object(self.agent,"_interactive_target",
                               return_value={"hwnd":123,"pid":77}), \
             mock.patch.object(self.agent,"_gui",return_value=fake_gui), \
             mock.patch.object(self.agent,"_target_window",
                               return_value={"foreground":True,"pid":77}), \
             mock.patch.object(self.agent,"window_focus") as focus:
            result=self.agent.terminal_send({
                "hwnd":123,"expected_pid":77,"title_contains":"Codex",
                "text":"Ping","submit":True,"allow_desktop_interference":True})
        self.assertTrue(result["submitted"])
        self.assertEqual(fake_gui.write.call_count,4)
        fake_gui.write.assert_has_calls([mock.call(ch,interval=0) for ch in "Ping"])
        fake_gui.press.assert_called_once_with("enter")
        focus.assert_not_called()


class ConcurrentAgentTests(TestCase):
    def test_default_four_slots(self):
        self.assertEqual(MAX_CONCURRENT_JOBS,4)

    def test_reads_overlap_long_commands_but_mutations_serialize(self):
        mutation_lock=threading.RLock()
        gui_lock=threading.RLock()
        active_mutators=0
        max_mutators=0
        state_lock=threading.Lock()
        started=threading.Event()
        class SlowFakeAgent:
            def handle_tool(self,tool,args):
                nonlocal active_mutators,max_mutators
                if tool=="run_command":
                    with state_lock:
                        active_mutators+=1
                        max_mutators=max(max_mutators,active_mutators)
                    started.set()
                    started.wait(.5)
                    import time
                    time.sleep(.16)
                    with state_lock:
                        active_mutators-=1
                return {"content":[{"type":"text","text":tool}],"isError":False}
        agent=SlowFakeAgent()
        with ThreadPoolExecutor(max_workers=4) as pool:
            a=pool.submit(execute_agent_job,agent,"run_command",{},mutation_lock,gui_lock)
            started.wait(1)
            b=pool.submit(execute_agent_job,agent,"run_command",{},mutation_lock,gui_lock)
            c=pool.submit(execute_agent_job,agent,"read_file",{},mutation_lock,gui_lock)
            self.assertEqual(c.result(timeout=.1)["content"][0]["text"],"read_file")
            a.result(timeout=3)
            b.result(timeout=3)
        self.assertEqual(max_mutators,1)


class PerformanceAndTraceTests(TestCase):
    def test_only_telemetry_uses_rapid_sqlite_sync(self):
        with tempfile.TemporaryDirectory() as td:
            hub=Hub(base_url="http://127.0.0.1:9999",data_dir=td,test_mode=True)
            with hub.db() as db:
                self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0],"wal")
                self.assertEqual(db.execute("PRAGMA synchronous").fetchone()[0],2)
            with hub.db(telemetry=True) as db:
                self.assertEqual(db.execute("PRAGMA synchronous").fetchone()[0],1)
            with hub.db() as db:
                self.assertEqual(db.execute("PRAGMA synchronous").fetchone()[0],2)

    def test_private_metrics_and_7day_retention(self):
        with tempfile.TemporaryDirectory() as td:
            hub=Hub(base_url="http://127.0.0.1:9999",data_dir=td,test_mode=True)
            u1=hub.register_user("perf-one@example.com","ExtremelyLongPassword1!")
            u2=hub.register_user("perf-two@example.com","ExtremelyLongPassword2!")
            job={"device_id":"d_x","tool":"read_file",
                 "enqueued_at":100.0,"dispatched_at":100.7,
                 "completed_at":101.0,"status":"ok"}
            hub.performance_record(u1["id"],job,101.3)
            s1=hub.performance_summary(u1["id"])
            s2=hub.performance_summary(u2["id"])
            self.assertEqual(s1["sample_count"],1)
            self.assertEqual(s2["sample_count"],0)
            self.assertAlmostEqual(s1["groups"][0]["queue"]["p50_ms"],700,delta=1)
            self.assertAlmostEqual(s1["groups"][0]["agent"]["p50_ms"],300,delta=1)
            self.assertEqual(s1["groups"][0]["errors"],0)

    def test_lossless_large_trace_compression(self):
        with tempfile.TemporaryDirectory() as td:
            trace=TrafficTrace(Path(td),enabled=True)
            payload={"result":{"content":[{"type":"text","text":"Данные"*12000}],
                               "isError":False}}
            trace.record(direction="agent_to_hub",route="/agent/result",user_id="u_test",
                         device_id="d_test",request_id="r_test",payload=payload)
            f=trace.path_for("u_test","d_test")
            blob=f.read_bytes()
            self.assertLess(len(blob),len(json.dumps(payload,ensure_ascii=False).encode())//2)
            raw=json.loads(blob)
            self.assertEqual(trace.decode_payload(raw["payload"]),payload)
            self.assertEqual(raw["request_id"],"r_test")


if __name__=="__main__":
    main()
