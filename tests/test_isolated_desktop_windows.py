"""Native Windows-only GUI Engine integration tests with a disposable test EXE.

No VM, games, global SendInput or physical mouse movement.
Run on an interactive Windows session where the user isn't moving the pointer.
"""
from pathlib import Path
import importlib
import json
import os
import sys
import tempfile
import time
import unittest

ROOT=Path(__file__).resolve().parents[1]

@unittest.skipUnless(os.name=="nt","Real isolated Win32 desktops require Windows")
class WindowsHiddenDesktopTests(unittest.TestCase):
    def test_full_native_desktop_lifecycle(self):
        sys.path.insert(0,str(ROOT/"client"))
        import asc_isolated_desktop as backend

        original=backend.desktop_snapshot()
        manager=backend.IsolatedDesktops()
        fixture=ROOT/"tests"/"fixtures"/"native_win32_gui.py"
        self.assertTrue(fixture.is_file())
        with tempfile.TemporaryDirectory() as folder:
            prefix=Path(folder)/"native-probe"
            sess=manager.create()
            try:
                live=manager.launch(sess["desktop_id"],
                    [sys.executable,str(fixture),str(prefix),sess["desktop_name"]],
                    cwd=str(Path(folder)))
                self.assertTrue(live["running"])
                ready=Path(folder)/"native-probe-ready.json"
                for _ in range(60):
                    if ready.is_file():break
                    time.sleep(.15)
                self.assertTrue(ready.is_file(),"Fixture did not start")
                details=json.loads(ready.read_text(encoding="utf8"))
                self.assertEqual(details["actual_desktop"],sess["desktop_name"],
                                 "Fixture unexpectedly launched on user's default desktop")
                result=manager.windows(sess["desktop_id"])
                self.assertEqual(result["active_processes"],[details["pid"]])
                e=next(x for x in result["windows"] if x["hwnd"]==details["edit_hwnd"])
                b=next(x for x in result["windows"] if x["hwnd"]==details["button_hwnd"])
                with self.assertRaises(PermissionError):
                    manager.action(sess["desktop_id"],e["hwnd"],details["pid"]+17,
                                   "set_text",text="no")
                edit=manager.action(sess["desktop_id"],e["hwnd"],details["pid"],
                                    "set_text",text="ASC GUI background test",
                                    expected_class="Edit")
                self.assertTrue(edit["text_verified"])
                click=manager.action(sess["desktop_id"],b["hwnd"],details["pid"],
                                     "click_button",expected_class="Button")
                self.assertTrue(click["dispatched"])
                time.sleep(.2)
                events=json.loads((Path(folder)/"native-probe-events.json").read_text())
                self.assertEqual(events["clicks"],1,"BM_CLICK did not invoke the window procedure")
                screenshot=manager.capture(sess["desktop_id"],details["hwnd"],details["pid"])
                self.assertTrue(screenshot["printwindow_returned"])
                self.assertFalse(screenshot["uniform_image"])
                self.assertGreater(screenshot["png_bytes"],100)
                self.assertEqual(original,backend.desktop_snapshot())
            finally:
                status=manager.close(sess["desktop_id"])
                self.assertTrue(status["job_handle_closed"],status)
                self.assertTrue(status["desktop_handle_closed"],status)
                self.assertFalse(manager.sessions()["sessions"])
                self.assertEqual(original,backend.desktop_snapshot())

if __name__=="__main__":
    unittest.main()
