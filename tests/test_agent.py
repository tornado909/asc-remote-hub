import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "client"))
from asc_remote_commander import Agent, AccessDenied, dispatch, windows_input_events


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "allowed"
        self.root.mkdir()
        self.other = Path(self.tmp.name) / "outside"
        self.other.mkdir()
        env = {"ASC_RC_ROOTS": str(self.root), "ASC_RC_STATE_DIR": str(Path(self.tmp.name) / "state"),
               "ASC_RC_ALLOW_WRITE": "", "ASC_RC_ALLOW_EXEC": "",
               "ASC_RC_ALLOW_DELETE": "", "ASC_RC_ALLOW_GUI": ""}
        self.envpatch = patch.dict(os.environ, env)
        self.envpatch.start()
        self.addCleanup(self.envpatch.stop)

    def test_windows_unicode_multiline_event_mapping(self):
        events=windows_input_events("Привет!\r\nМир!\n😀\t")
        self.assertEqual(sum(vk==0x0D and flags==0 for vk,scan,flags in events),2)
        self.assertEqual(sum(vk==0x0D and flags==0x0002 for vk,scan,flags in events),2)
        self.assertEqual(sum(vk==0x09 and flags==0 for vk,scan,flags in events),1)
        self.assertFalse(any(vk==0 and scan in (10,13,9) for vk,scan,flags in events))
        self.assertTrue(any(vk==0 and scan==0xD83D for vk,scan,flags in events))
        self.assertTrue(any(vk==0 and scan==0xDE00 for vk,scan,flags in events))
        self.assertEqual(windows_input_events("a\n\rb"),windows_input_events("a\n\nb"))
        self.assertEqual(windows_input_events("a\r\nb"),
                         windows_input_events("a\nb"))

    def test_default_read_only(self):
        agent = Agent()
        names = agent.tools.keys()
        self.assertIn("machine_info", names)
        self.assertNotIn("run_command", names)
        self.assertNotIn("write_file", names)
        response = dispatch(agent, {"jsonrpc": "2.0", "id": 4, "method": "initialize",
                                    "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(response["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("tools", response["result"]["capabilities"])
        self.assertIsNone(dispatch(agent, {"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_roots_and_traversal_and_symlink(self):
        agent = Agent()
        with self.assertRaises(AccessDenied):
            agent.path(str(self.other / "secret.txt"))
        with self.assertRaises(ValueError):
            agent.path("../outside")
        with self.assertRaises(AccessDenied):
            agent.path(str(self.root / ".." / "outside" / "secret"))
        try:
            (self.root / "external").symlink_to(self.other, target_is_directory=True)
        except OSError:
            return  # Windows non-developer mode may not permit symlinks.
        with self.assertRaises(AccessDenied):
            agent.path(str(self.root / "external" / "secret.txt"))

    def test_read_write_replace_and_refuse_overwrite(self):
        os.environ["ASC_RC_ALLOW_WRITE"] = "1"
        agent = Agent()
        path = self.root / "русский.txt"
        agent.write_file({"path": str(path), "text": "привет\nстрока 2\n"})
        with self.assertRaises(FileExistsError):
            agent.write_file({"path": str(path), "text": "overwrite"})
        data = agent.read_file({"path": str(path), "offset": 0, "limit": 1})
        self.assertEqual(data["text"], "привет\n")
        self.assertEqual(data["next_offset"], 1)
        with self.assertRaises(ValueError):
            agent.replace_text({"path": str(path), "old_text": "строка", "new_text": "line", "expected_count": 2})
        self.assertIn("строка", path.read_text(encoding="utf-8"))
        agent.replace_text({"path": str(path), "old_text": "строка", "new_text": "line", "expected_count": 1})
        self.assertEqual(path.read_text(encoding="utf-8"), "привет\nline 2\n")
        agent.write_file({"path": str(path), "text": "extra", "mode": "append"})
        self.assertTrue(path.read_text(encoding="utf-8").endswith("extra"))

    def test_command_opt_in_and_no_shell_interpretation(self):
        agent = Agent()
        response = agent.handle_tool("run_command", {"program": sys.executable, "args": ["-V"]})
        self.assertTrue(response["isError"])
        os.environ["ASC_RC_ALLOW_EXEC"] = "1"
        agent = Agent()
        res = agent.run_command({"program": sys.executable, "args": ["-c", "print('ok')"]})
        self.assertEqual(res["exit_code"], 0)
        self.assertEqual(res["output"].strip(), "ok")

    def test_job_lifecycle(self):
        os.environ["ASC_RC_ALLOW_EXEC"] = "1"
        agent = Agent()
        job = agent.start_process({"program": sys.executable,
                                   "args": ["-u", "-c", "print('started'); import time; time.sleep(10)"]})
        self.assertIn("job_id", job)
        response = agent.read_process({"job_id": job["job_id"]})
        self.assertIn("running", response)
        agent.stop_process({"job_id": job["job_id"]})
        self.assertFalse(agent.read_process({"job_id": job["job_id"]})["running"])

    def test_audit_does_not_contain_arguments(self):
        agent = Agent()
        marker = "MY_TEST_SECRET_01234"
        agent.handle_tool("stat_path", {"path": marker})
        audit = (agent.state_dir / "audit.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(marker, audit)
        self.assertIn("stat_path", audit)

    def test_stdio_integration(self):
        env = dict(os.environ)
        p = subprocess.run([sys.executable, str(ROOT / "client" / "asc_remote_commander.py")],
                           input='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25"}}\n'
                                 '{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
                                 '{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n'
                                 '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"machine_info","arguments":{}}}\n',
                           text=True, capture_output=True, env=env, timeout=5)
        self.assertEqual(p.returncode, 0, p.stderr)
        parsed = [json.loads(x) for x in p.stdout.splitlines()]
        self.assertEqual(len(parsed), 3)
        self.assertEqual(parsed[0]["result"]["protocolVersion"], "2025-11-25")
        self.assertTrue(any(x["name"] == "machine_info" for x in parsed[1]["result"]["tools"]))
        self.assertFalse(parsed[2]["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
