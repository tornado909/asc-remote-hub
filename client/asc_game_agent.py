"""Optional ASC Game Agent v18/v19 bridge. Windows-only and fail closed.

Uses the *already installed* sealed per-user packages. The Hub never ships
third-party DirectML/ONNX binaries, installs hooks, launches games or
approves the native capture consent dialog.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Callable


class GameAgentError(RuntimeError):
    """A Game Agent package, capability or safety-contract failure."""


def _int(value: object, minimum: int, maximum: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer within {minimum}..{maximum}")
    return value


def _hwnd(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"(?:0[xX][0-9a-fA-F]{1,16}|[0-9]{1,20})", value):
        raise ValueError("hwnd must be the numeric handle supplied by game_agent_targets")
    n = int(value, 16 if value.lower().startswith("0x") else 10)
    if n < 1 or n > 0x7fffffffffffffff:
        raise ValueError("hwnd is out of range")
    return f"0x{n:x}"


class GameAgentBridge:
    TARGETS = "windows-targets-v18"
    CAPTURE = "windows-consented-capture-v19"
    SCRIPTS = {
        TARGETS: "Run-WindowTargets.ps1",
        CAPTURE: "Run-ConsentedCapture.ps1",
    }

    def __init__(self, root: Path | None = None, *,
                 runner: Callable[..., Any] = subprocess.run,
                 platform: str | None = None) -> None:
        self.platform = platform or os.name
        # The sole root is the current user's installed Game Agent. Never accept
        # remote-supplied paths, PowerShell expressions, executable or model paths.
        if root is None:
            local = os.environ.get("LOCALAPPDATA", "")
            root = Path(local) / "ASC-Game-Agent" if local else Path.home() / "AppData" / "Local" / "ASC-Game-Agent"
        self.root = Path(root)
        self.runner = runner

    def _script(self, package: str) -> Path:
        if self.platform != "nt":
            raise GameAgentError("GAME_AGENT_WINDOWS_ONLY")
        script = self.root / "extensions" / package / self.SCRIPTS[package]
        if not script.is_file() or script.is_symlink():
            raise GameAgentError(f"GAME_AGENT_PACKAGE_MISSING_OR_UNTRUSTED:{package}")
        # The native PowerShell wrapper checks the sealed SHA-256 manifest of
        # the *entire* extension before executing any native code.
        return script

    @staticmethod
    def _console_encoding() -> str:
        # Windows PowerShell 5.1 emits ConvertTo-Json through the active
        # *console output codepage* even when its native child used UTF-8.
        # GetConsoleOutputCP (not locale.getpreferredencoding / ANSI CP)
        # preserves Cyrillic window titles on CP866 machines.
        import ctypes
        k = ctypes.windll.kernel32
        cp = k.GetConsoleOutputCP() or k.GetOEMCP()
        return f"cp{cp}"

    def _call(self, package: str, action: str, args: list[str] | None = None, *,
              timeout: int = 26) -> dict[str, Any]:
        script = self._script(package)
        cmd = ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
               "-ExecutionPolicy", "Bypass", "-File", str(script), "-Action", action]
        if args:
            cmd.extend(args)
        try:
            proc = self.runner(cmd, capture_output=True, check=False,
                               timeout=timeout,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as exc:
            raise GameAgentError(f"GAME_AGENT_TIMEOUT:{action}") from exc
        except OSError as exc:
            raise GameAgentError(f"GAME_AGENT_BACKEND_UNAVAILABLE:{action}") from exc
        if proc.returncode:
            # Avoid echoing arbitrary process titles, local paths and stderr
            # into a remote error. Native worker has already failed closed.
            raise GameAgentError(f"GAME_AGENT_REJECTED:{package}:{action}:exit={proc.returncode}")
        output = proc.stdout
        if not isinstance(output, bytes) or len(output) > 250_000:
            raise GameAgentError("GAME_AGENT_INVALID_OUTPUT_SIZE")
        try:
            try:
                decoded = output.decode("utf-8-sig")
            except UnicodeDecodeError:
                if self.platform != "nt":
                    raise
                decoded = output.decode(self._console_encoding())
            result = json.loads(decoded.strip())
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise GameAgentError(f"GAME_AGENT_INVALID_JSON:{action}") from exc
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise GameAgentError(f"GAME_AGENT_UNEXPECTED_RESULT:{action}")
        return result

    @staticmethod
    def _no_capture(data: dict[str, Any], *, action: str) -> None:
        if data.get("capture_authorized") is not False:
            raise GameAgentError(f"GAME_AGENT_UNEXPECTED_CONSENT:{action}")
        if data.get("gpu_initialized", False) is True or data.get("window_captured", False) is True:
            raise GameAgentError(f"GAME_AGENT_UNEXPECTED_GPU_CAPTURE:{action}")

    def status(self) -> dict[str, Any]:
        """No target enumeration, no GPU and no capture."""
        if self.platform != "nt":
            return {"available": False, "reason": "WINDOWS_ONLY", "gpu_initialized": False}
        components = {}
        for package, name in ((self.TARGETS, "windows_v18"), (self.CAPTURE, "capture_v19")):
            try:
                data = self._call(package, "Doctor", timeout=35)
                if data.get("gpu_initialized") is not False:
                    raise GameAgentError("GAME_AGENT_DOCTOR_TOUCHED_GPU")
                if package == self.TARGETS:
                    self._no_capture(data, action="Doctor")
                elif data.get("screen_captured") is not False:
                    raise GameAgentError("GAME_AGENT_DOCTOR_CAPTURED_SCREEN")
                components[name] = {"installed": True, "verified": True, "version": data.get("version")}
            except GameAgentError as exc:
                components[name] = {"installed": False, "verified": False, "reason": str(exc)}
        return {"available": all(x["verified"] for x in components.values()),
                "perception": {"universal_scene_schema": "asc.game_agent.scene.v1",
                               "semantic_provider_ready": False,
                               "open_vocabulary": False,
                               "ocr": False,
                               "ui_grounding": False,
                               "diagnostic_only": True},
                "backend": "Win32 target fingerprint + user-consented WGC/DirectML",
                "research_only": True, "capture_requires_native_local_yes": True,
                "gpu_initialized": False, "input_sent": False, "components": components}

    def targets(self, limit: int = 50) -> dict[str, Any]:
        limit = _int(limit, 1, 100, "limit")
        data = self._call(self.TARGETS, "List", timeout=25)
        self._no_capture(data, action="List")
        if data.get("code") != "WINDOW_TARGETS_DISCOVERED" or not isinstance(data.get("windows"), list):
            raise GameAgentError("GAME_AGENT_TARGET_LIST_INVALID")
        result: list[dict[str, Any]] = []
        for w in data["windows"][:limit]:
            if not isinstance(w, dict) or w.get("capture_authorized") is not False:
                raise GameAgentError("GAME_AGENT_TARGET_IDENTITY_INVALID")
            hwnd = _hwnd(w.get("hwnd"))
            pid = _int(w.get("pid"), 1, 2**32 - 1, "pid")
            ticks = _int(w.get("process_start_ticks"), 1, 2**64 - 1, "process_start_ticks")
            # Whitelist metadata. Do not return full EXE path or raw native JSON.
            result.append({"hwnd": hwnd, "pid": pid, "process_start_ticks": ticks,
                           "title": str(w.get("title", ""))[:300],
                           "executable": str(w.get("image", ""))[:128],
                           "window_class": str(w.get("class", ""))[:200]})
        return {"ok": True, "targets": result, "total": len(data["windows"]),
                "truncated": len(data["windows"]) > limit,
                "capture_authorized": False, "gpu_initialized": False}

    def _chosen(self, hwnd: object, pid: object, ticks: object) -> tuple[str, int, int]:
        handle = _hwnd(hwnd)
        process = _int(pid, 1, 2**32 - 1, "pid")
        started = _int(ticks, 1, 2**64 - 1, "process_start_ticks")
        # Refresh identity immediately before each preflight/capture. Never
        # take a model-selected PID/handle on faith or fall back to the first.
        options = self.targets(limit=100)["targets"]
        if not any(x["hwnd"] == handle and x["pid"] == process
                   and x["process_start_ticks"] == started for x in options):
            raise GameAgentError("GAME_AGENT_TARGET_CHANGED")
        return handle, process, started

    def preflight(self, hwnd: object, pid: object, ticks: object) -> dict[str, Any]:
        handle, process, started = self._chosen(hwnd, pid, ticks)
        data = self._call(self.CAPTURE, "Preflight",
                          ["-Hwnd", handle, "-ProcessId", str(process),
                           "-ProcessStartTicks", str(started)], timeout=35)
        self._no_capture(data, action="Preflight")
        if data.get("code") != "TARGET_FINGERPRINT_PREFLIGHT_PASS":
            raise GameAgentError("GAME_AGENT_PREFLIGHT_REJECTED")
        return {"ok": True, "hwnd": handle, "pid": process,
                "process_start_ticks": started, "capture_authorized": False,
                "gpu_initialized": False, "local_consent_required": True}

    @staticmethod
    def _vision_result(report: dict[str, Any], *, expected_frames: int,
                       external: bool, target_pid: int | None = None) -> dict[str, Any]:
        actual = _int(report.get("frames_processed"), 1, expected_frames, "frames_processed")
        invariants = {
            "ok": True, "gpu_only": True, "cpu_fallback_disabled": True,
            "grant_persisted": False, "grant_valid_after_exit": False,
            "full_frames_to_cpu_bytes": 0, "tensor_to_cpu_bytes": 0,
            "logits_to_cpu_bytes": 0, "screen_recording_saved": False,
            "host_input_sent": False, "game_started": False,
            "model_sessions_created": 1, "identity_checked_per_frame": True,
            "metadata_to_cpu_bytes": actual * 64,
        }
        if any(report.get(k) != v for k, v in invariants.items()):
            raise GameAgentError("GAME_AGENT_VISION_SAFETY_CONTRACT_FAILED")
        expected_scope = "user_selected_window" if external else "self_fixture"
        expected_consent = "native_local_dialog" if external else "self_fixture_test"
        if report.get("capture_target") != expected_scope or report.get("consent_origin") != expected_consent:
            raise GameAgentError("GAME_AGENT_CAPTURE_SCOPE_INVALID")
        if external and (report.get("target_pid") != target_pid or report.get("diagnostic_pixels_to_cpu_bytes") != 0):
            raise GameAgentError("GAME_AGENT_EXTERNAL_TARGET_REPORT_INVALID")
        frames = report.get("frames")
        if not isinstance(frames, list) or len(frames) != actual:
            raise GameAgentError("GAME_AGENT_FRAME_COUNT_MISMATCH")
        vision: list[dict[str, Any]] = []
        for index, raw in enumerate(frames):
            if not isinstance(raw, dict):
                raise GameAgentError("GAME_AGENT_FRAME_RESULT_INVALID")
            confidences = {}
            for label in ("person", "bus"):
                score = raw.get(f"{label}_confidence")
                if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 1:
                    raise GameAgentError("GAME_AGENT_SCORE_INVALID")
                confidences[label] = float(score)
            vision.append({"frame_index": index,
                           "taxonomy": "COCO80", "signals": confidences})
        return {
            "schema": "asc.game_agent.vision_result.v1",
            "ok": True, "backend": "windows.wgc.directml.v19",
            "research_only": True, "frames_processed": actual,
            "processed_fps": report.get("processed_fps"),
            "capture_scope": expected_scope, "consent_origin": expected_consent,
            "model": "YOLO11n-COCO80", "game_semantics_available": False,
            "diagnostic_only": True,
            "full_frames_to_cpu_bytes": 0, "gpu_only": True,
            "input_sent": False, "persistent_grant": False,
            "frames": vision,
        }

    def capture(self, hwnd: object, pid: object, ticks: object, frames: int = 12) -> dict[str, Any]:
        if os.environ.get("ASC_RC_ALLOW_GAME_AGENT_CAPTURE", "").lower() not in ("1", "true", "yes", "on"):
            raise GameAgentError("GAME_AGENT_CAPTURE_LOCALLY_DISABLED")
        frames = _int(frames, 2, 32, "frames")
        handle, process, started = self._chosen(hwnd, pid, ticks)
        # Explicit NO-CAPTURE preflight before native TaskDialog. Native v19
        # enforces a local YES click or denies; no auto-approval path exists.
        self.preflight(handle, process, started)
        report = self._call(self.CAPTURE, "Capture",
                            ["-Hwnd", handle, "-ProcessId", str(process),
                             "-ProcessStartTicks", str(started),
                             "-Frames", str(frames)], timeout=46)
        return self._vision_result(report, expected_frames=frames, external=True,
                                   target_pid=process)

    def fixture(self, frames: int = 8) -> dict[str, Any]:
        """Only captures the native worker's OWN synthetic temporary window."""
        if os.environ.get("ASC_RC_ALLOW_GAME_AGENT_GPU_TEST", "").lower() not in ("1", "true", "yes", "on"):
            raise GameAgentError("GAME_AGENT_GPU_TEST_LOCALLY_DISABLED")
        frames = _int(frames, 2, 32, "frames")
        report = self._call(self.CAPTURE, "Fixture",
                            ["-Frames", str(frames)], timeout=46)
        return self._vision_result(report, expected_frames=frames, external=False)
