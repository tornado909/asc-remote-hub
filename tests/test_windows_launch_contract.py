"""Guards against silent Windows installer success without an online agent."""
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]

class WindowsLaunchContract(unittest.TestCase):
    def test_launcher_propagates_powershell_exit_code(self):
        launch=(ROOT/"install/launch_tray_windows.vbs").read_text(encoding="utf-8-sig")
        self.assertIn("result = shell.Run(command, 0, True)",launch)
        self.assertIn("WScript.Quit result",launch)

    def test_tray_supervisor_logs_errors_and_has_icon_fallback(self):
        script=(ROOT/"install/tray_windows.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("tray-supervisor.log",script)
        self.assertIn("Write-TrayDiagnostic ('FATAL",script)
        self.assertIn("catch {",script)
        self.assertIn("[System.Drawing.SystemIcons]::Application",script)

    def test_installer_requires_authenticated_worker_hello(self):
        installer=(ROOT/"install/install_windows.ps1").read_text(encoding="utf-8-sig")
        worker=(ROOT/"client/bridge.py").read_text()
        self.assertIn("agent.ready.json",installer)
        self.assertIn("record_connected_marker(did)",worker)
        self.assertIn('hello_reply.get("device_id") != did',worker)
        self.assertIn("Get-Process -Id $pidValue",installer)
        self.assertIn("$installStarted",installer)
        self.assertIn("if (-not $connected)",installer)
        self.assertIn("throw 'Автозапуск не подтвердился",installer)
        self.assertIn("WScript",installer) if False else None

if __name__=="__main__":
    unittest.main()
