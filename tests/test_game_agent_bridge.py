"""ASC Game Agent bridge safety contract; no Windows/GPU needed for CI."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "client"))
sys.path.insert(0, str(HERE / "server"))
from asc_game_agent import GameAgentBridge, GameAgentError

HANDLE = "0x123"
PID = 4444
START = 133000000000000000


def listed():
    return {"ok": True, "code": "WINDOW_TARGETS_DISCOVERED",
            "capture_authorized": False,
            "windows": [{"hwnd": HANDLE, "pid": PID, "tid": 123,
                         "session": 1, "process_start_ticks": START,
                         "image": "Game.exe", "class": "UnrealWindow",
                         "title": "Тестовое окно", "capture_authorized": False,
                         "private_unexpected": "must never escape"}]}


def report(scope="user_selected_window", frames=2):
    is_external = scope == "user_selected_window"
    return {"ok": True, "gpu_only": True, "cpu_fallback_disabled": True,
            "grant_persisted": False, "grant_valid_after_exit": False,
            "full_frames_to_cpu_bytes": 0, "tensor_to_cpu_bytes": 0,
            "logits_to_cpu_bytes": 0, "screen_recording_saved": False,
            "host_input_sent": False, "game_started": False,
            "model_sessions_created": 1, "identity_checked_per_frame": True,
            "metadata_to_cpu_bytes": frames * 64, "frames_processed": frames,
            "capture_target": scope,
            "consent_origin": "native_local_dialog" if is_external else "self_fixture_test",
            "target_pid": PID, "diagnostic_pixels_to_cpu_bytes": 0,
            "processed_fps": 21.5,
            "frames": [{"person_confidence": 0.12, "bus_confidence": 0.04,
                        "test_pixel_rgba": [100, 200, 0, 255]} for _ in range(frames)],
            "untrusted_raw": "never publish full worker payload"}


def v22_report():
    detections=[{"prompt_index":i,"score":0.001,"anchor":i,
                 "box_model_normalized":[.1,.2,.3,.4]}
                for i in range(8)]
    detections[0]["score"]=.75
    return {
        "ok":True,"code":"WINDOW_CAPTURE_SESSION_COMPLETED",
        "version":"22-open-vocabulary-canary",
        "mode":"consented_selected_window_WGC_DirectML_OpenVocab",
        "semantic_provider":"YOLO-World-v2","open_vocabulary":True,
        "runtime_text_embedding_classes":8,
        "capture_target":"user_selected_window","consent_origin":"native_local_dialog",
        "capture_authorized_during_session":True,
        "identity_checked_per_frame":True,"grant_persisted":False,
        "grant_valid_after_exit":False,"gpu_only":True,
        "cpu_fallback_disabled":True,"screen_recording_saved":False,
        "game_started":False,"host_input_sent":False,
        "full_frames_to_cpu_bytes":0,"tensor_to_cpu_bytes":0,
        "logits_to_cpu_bytes":0,"text_embedding_cpu_upload_bytes":16384,
        "model_sessions_created":1,"max_frames_in_flight":1,
        "metadata_to_cpu_bytes":512,"diagnostic_pixels_to_cpu_bytes":0,
        "wgc_input_last_width":1280,"wgc_input_last_height":720,
        "gpu_tensor_width":1280,"gpu_tensor_height":720,
        "aspect_ratio_preserved":True,
        "target_pid":PID,"target_hwnd":HANDLE,
        "frames_processed":2,
        "frames":[{"index":i,"detections":[dict(item) for item in detections],
                   "private_gpu_tensor":"not transmitted"}
                  for i in range(2)],
    }


class FakeWorker:
    def __init__(self):
        self.calls = []
        self.responses = {
            ("windows-targets-v18", "Doctor"):
                {"ok": True, "capture_authorized": False, "gpu_initialized": False,
                 "version": "18-canary"},
            ("windows-consented-capture-v19", "Doctor"):
                {"ok": True, "screen_captured": False, "gpu_initialized": False,
                 "version": "19-canary"},
            ("windows-targets-v18", "List"): listed(),
            ("windows-consented-capture-v19", "Preflight"):
                {"ok": True, "code": "TARGET_FINGERPRINT_PREFLIGHT_PASS",
                 "capture_authorized": False, "gpu_initialized": False,
                 "window_captured": False},
            ("windows-consented-capture-v19", "Capture"): report(),
            ("windows-consented-capture-v19", "Fixture"): report("self_fixture"),
            ("windows-universal-vision-v22-runtime", "Capture"): v22_report(),
            ("windows-universal-vision-v22-runtime", "Doctor"):
                {"ok":True,"code":"ASC_V22_PRIVATE_CANARY_VERIFIED",
                 "version":"22-private","gpu_initialized":False,
                 "captured":False,"input_sent":False},
        }

    def __call__(self, args, *, capture_output, check, timeout, creationflags):
        self.calls.append((args, timeout))
        action = args[args.index("-Action") + 1]
        filename = args[args.index("-File") + 1]
        package = ("windows-targets-v18" if "WindowTargets" in filename else
                   "windows-universal-vision-v22-runtime" if "UniversalVision" in filename else
                   "windows-consented-capture-v19")
        result = self.responses[(package, action)]
        if isinstance(result, bytes):
            output = result
        else:
            output = json.dumps(result, ensure_ascii=False).encode("utf8")
        return SimpleNamespace(returncode=0, stdout=output, stderr=b"")


class GameAgentBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name) / "GameAgent"
        for name, script in GameAgentBridge.SCRIPTS.items():
            target = root / "extensions" / name / script
            target.parent.mkdir(parents=True)
            target.write_text("# fake sealed extension for mocked runner", encoding="utf8")
        self.worker = FakeWorker()
        self.backend = GameAgentBridge(root=root, runner=self.worker, platform="nt")
        self.root = root
        self.v22_models = root/"extensions"/"windows-universal-vision-v22-runtime"/"models"
        self.v22_models.mkdir(parents=True, exist_ok=True)
        features=bytes(16384)
        (self.v22_models/"yoloworld_clip8_features.bin").write_bytes(features)
        (self.v22_models/"yoloworld_clip8_features.json").write_text(
            json.dumps({"model":"openai/clip-vit-base-patch32",
                        "tensor_shape":[1,8,512],
                        "sha256":hashlib.sha256(features).hexdigest(),
                        "prompts":["figure","animal","rock","tree","icon",
                                   "entry","tool","structure"]}),encoding="utf8")
        self.flags = patch.dict(os.environ, {"ASC_RC_ALLOW_GAME_AGENT_CAPTURE": "",
                                           "ASC_RC_ALLOW_GAME_AGENT_GPU_TEST": "",
                                           "ASC_RC_ALLOW_GAME_AGENT_OPEN_VOCAB": ""})
        self.flags.start()
        self.addCleanup(self.flags.stop)

    def test_doctor_only_no_capture(self):
        status = self.backend.status()
        self.assertTrue(status["available"])
        self.assertFalse(status["gpu_initialized"])
        self.assertFalse(status["input_sent"])
        self.assertFalse(status["perception"]["semantic_provider_ready"])
        self.assertFalse(status["perception"]["open_vocabulary"])
        self.assertFalse(status["perception"]["ocr"])
        self.assertTrue(status["perception"]["diagnostic_only"])
        self.assertEqual(status["perception"]["universal_scene_schema"],
                         "asc.game_agent.scene.v1")
        self.assertEqual([x[0][x[0].index("-Action")+1] for x in self.worker.calls],
                         ["Doctor","Doctor"])

    def test_list_is_metadata_only_unicode_preserved(self):
        info = self.backend.targets()
        self.assertEqual(info["targets"][0]["title"], "Тестовое окно")
        self.assertEqual(info["targets"][0]["hwnd"], HANDLE)
        self.assertNotIn("private_unexpected",str(info))
        self.assertFalse(info["capture_authorized"])
        self.assertEqual(len(self.worker.calls),1)

    def test_preflight_rechecks_full_fingerprint_and_stays_readonly(self):
        ok = self.backend.preflight(HANDLE,PID,START)
        self.assertTrue(ok["ok"])
        self.assertFalse(ok["capture_authorized"])
        self.assertTrue(ok["local_consent_required"])
        self.assertEqual([x[0][x[0].index("-Action")+1] for x in self.worker.calls],
                         ["List","Preflight"])
        before = len(self.worker.calls)
        with self.assertRaisesRegex(GameAgentError,"TARGET_CHANGED"):
            self.backend.preflight(HANDLE,PID,START+1)
        self.assertEqual(len(self.worker.calls),before+1)  # ONLY list, not v19
        with self.assertRaisesRegex(ValueError,"hwnd"):
            self.backend.preflight("0x123;whoami",PID,START)

    def test_local_capture_opt_in_is_required_before_any_work(self):
        with self.assertRaisesRegex(GameAgentError,"CAPTURE_LOCALLY_DISABLED"):
            self.backend.capture(HANDLE,PID,START,frames=2)
        self.assertEqual(self.worker.calls,[])

    def test_capture_returns_small_coco_metadata_no_pixels(self):
        with patch.dict(os.environ, {"ASC_RC_ALLOW_GAME_AGENT_CAPTURE":"1"}):
            result = self.backend.capture(HANDLE,PID,START,frames=2)
        self.assertEqual(result["schema"],"asc.game_agent.vision_result.v1")
        self.assertEqual(len(result["frames"]),2)
        self.assertEqual(result["frames"][0]["taxonomy"],"COCO80")
        self.assertFalse(result["game_semantics_available"])
        self.assertTrue(result["diagnostic_only"])
        self.assertEqual(result["full_frames_to_cpu_bytes"],0)
        self.assertNotIn("test_pixel_rgba",str(result))
        self.assertNotIn("untrusted_raw",str(result))
        self.assertNotIn("report_path",str(result))
        self.assertEqual([x[0][x[0].index("-Action")+1] for x in self.worker.calls],
                         ["List","List","Preflight","Capture"])

    def test_capture_rejects_fake_consent_and_cpu_fallback(self):
        with patch.dict(os.environ, {"ASC_RC_ALLOW_GAME_AGENT_CAPTURE":"1"}):
            bad = report()
            bad["consent_origin"]="self_fixture_test"
            self.worker.responses[("windows-consented-capture-v19","Capture")]=bad
            with self.assertRaisesRegex(GameAgentError,"SCOPE_INVALID"):
                self.backend.capture(HANDLE,PID,START,frames=2)
            bad = report()
            bad["full_frames_to_cpu_bytes"]=1024
            self.worker.responses[("windows-consented-capture-v19","Capture")]=bad
            with self.assertRaisesRegex(GameAgentError,"SAFETY_CONTRACT_FAILED"):
                self.backend.capture(HANDLE,PID,START,frames=2)

    def test_fixture_requires_independent_local_opt_in(self):
        with self.assertRaisesRegex(GameAgentError,"GPU_TEST_LOCALLY_DISABLED"):
            self.backend.fixture(frames=2)
        self.assertEqual(self.worker.calls,[])
        with patch.dict(os.environ, {"ASC_RC_ALLOW_GAME_AGENT_GPU_TEST":"1"}):
            result = self.backend.fixture(frames=2)
        self.assertEqual(result["capture_scope"],"self_fixture")
        self.assertFalse(result["input_sent"])
        self.assertEqual(len(self.worker.calls),1)

    def test_worker_never_receives_shell_or_user_controlled_paths(self):
        with patch.dict(os.environ, {"ASC_RC_ALLOW_GAME_AGENT_GPU_TEST":"1"}):
            self.backend.fixture(frames=2)
        call=self.worker.calls[0][0]
        self.assertEqual(call[0],"powershell.exe")
        self.assertIn("-NonInteractive",call)
        self.assertNotIn("-Command",call)
        self.assertNotIn("-ModelPath",call)
        self.assertNotIn("-ReportPath",call)
        self.assertNotIn("SendInput",str(call))

    def test_failclosed_malformed_worker_or_wrong_platform(self):
        self.worker.responses[("windows-targets-v18","List")]=b"Not JSON"
        with self.assertRaisesRegex(GameAgentError,"INVALID_JSON"):
            self.backend.targets()
        self.assertFalse(GameAgentBridge(root=self.backend.root,runner=self.worker,
                                         platform="posix").status()["available"])
        with self.assertRaisesRegex(GameAgentError,"WINDOWS_ONLY"):
            GameAgentBridge(root=self.backend.root,runner=self.worker,
                            platform="posix").targets()

    def test_bad_vision_metadata_refused(self):
        bad=report()
        bad["frames"][1]["person_confidence"]=2.0
        with self.assertRaisesRegex(GameAgentError,"SCORE_INVALID"):
            self.backend._vision_result(bad,expected_frames=2,external=True,target_pid=PID)
        bad=report()
        bad["identity_checked_per_frame"]=False
        with self.assertRaisesRegex(GameAgentError,"SAFETY_CONTRACT_FAILED"):
            self.backend._vision_result(bad,expected_frames=2,external=True,target_pid=PID)

    def test_v22_open_vocab_needs_independent_opt_in_and_native_scope(self):
        with patch.dict(os.environ, {"ASC_RC_ALLOW_GAME_AGENT_CAPTURE":"1",
                                     "ASC_RC_ALLOW_GAME_AGENT_OPEN_VOCAB":"0"}):
            with self.assertRaisesRegex(GameAgentError,"V22_LOCALLY_DISABLED"):
                self.backend.open_vocab(HANDLE,PID,START,frames=2)
            self.assertEqual(self.worker.calls,[])
        with patch.dict(os.environ, {"ASC_RC_ALLOW_GAME_AGENT_CAPTURE":"1",
                                     "ASC_RC_ALLOW_GAME_AGENT_OPEN_VOCAB":"1"}):
            status=self.backend.status()
            self.assertTrue(status["perception"]["open_vocabulary_canary_ready"])
            self.assertFalse(status["perception"]["semantic_provider_ready"])
            result=self.backend.open_vocab(HANDLE,PID,START,frames=2,threshold=.2)
            self.assertTrue(result["ok"])
            self.assertEqual(result["schema"],"asc.game_agent.perception_batch.v1")
            self.assertEqual(len(result["scenes"]),2)
            self.assertEqual(result["scenes"][0]["entities"][0]["label"],"figure")
            self.assertFalse(result["scenes"][0]["action_authorized"])
            self.assertFalse(result["game_semantics_verified"])
            self.assertEqual(result["capture_scope"],"user_selected_window")
            self.assertNotIn("private_gpu_tensor",str(result))
            self.assertNotIn("sha256",str(result))
            actions=[x[0][x[0].index("-Action")+1] for x in self.worker.calls]
            self.assertIn("Capture",actions)
            self.assertEqual(actions[-1],"Capture")
            capture_args=self.worker.calls[-1][0]
            self.assertIn("-ProcessStartTicks",capture_args)
            self.assertNotIn("-ModelPath",capture_args)
            self.assertNotIn("-Embeddings",capture_args)
            self.assertNotIn("-ReportPath",capture_args)

    def test_v22_rejects_fake_native_local_consent_without_exposing_report(self):
        with patch.dict(os.environ, {"ASC_RC_ALLOW_GAME_AGENT_CAPTURE":"1",
                                     "ASC_RC_ALLOW_GAME_AGENT_OPEN_VOCAB":"1"}):
            report=v22_report()
            report["consent_origin"]="self_fixture_test"
            self.worker.responses[("windows-universal-vision-v22-runtime","Capture")]=report
            with self.assertRaisesRegex(GameAgentError,"UNTRUSTED_METADATA"):
                self.backend.open_vocab(HANDLE,PID,START,frames=2)
            report=v22_report()
            report["full_frames_to_cpu_bytes"]=1024
            self.worker.responses[("windows-universal-vision-v22-runtime","Capture")]=report
            with self.assertRaisesRegex(GameAgentError,"UNTRUSTED_METADATA"):
                self.backend.open_vocab(HANDLE,PID,START,frames=2)

    def test_powershell_51_oem_codepage_roundtrip(self):
        sample = listed()
        self.worker.responses[("windows-targets-v18","List")] = (
            json.dumps(sample,ensure_ascii=False).encode("cp866"))
        with patch.object(GameAgentBridge, "_console_encoding", return_value="cp866"):
            data=self.backend.targets()
        self.assertEqual(data["targets"][0]["title"],"Тестовое окно")

    def test_agent_registers_only_on_explicit_local_policy(self):
        from asc_remote_commander import Agent
        with patch.dict(os.environ, {"ASC_RC_ROOTS":self.tmp.name,
                                     "ASC_RC_STATE_DIR":str(Path(self.tmp.name)/"audit"),
                                     "ASC_RC_ALLOW_GUI":"1","ASC_RC_ALLOW_EXEC":"1",
                                     "ASC_RC_ALLOW_GAME_AGENT":"1"}):
            agent=Agent()  # linux initialization first, so Path remains valid
            with patch("asc_remote_commander.os.name","nt"):
                agent._register_tools()
            for name in ("game_agent_status","game_agent_targets",
                         "game_agent_preflight","game_agent_capture",
                         "game_agent_gpu_selftest"):
                self.assertIn(name,agent.tools)
            self.assertNotIn("game_agent_open_vocab",agent.tools)
        with patch.dict(os.environ, {"ASC_RC_ROOTS":self.tmp.name,
                                     "ASC_RC_STATE_DIR":str(Path(self.tmp.name)/"audit-v22"),
                                     "ASC_RC_ALLOW_GUI":"1","ASC_RC_ALLOW_EXEC":"1",
                                     "ASC_RC_ALLOW_GAME_AGENT":"1",
                                     "ASC_RC_ALLOW_GAME_AGENT_CAPTURE":"1",
                                     "ASC_RC_ALLOW_GAME_AGENT_OPEN_VOCAB":"1"}):
            agent=Agent()
            with patch("asc_remote_commander.os.name","nt"):
                agent._register_tools()
            self.assertIn("game_agent_open_vocab",agent.tools)
        with patch.dict(os.environ, {"ASC_RC_ROOTS":self.tmp.name,
                                     "ASC_RC_STATE_DIR":str(Path(self.tmp.name)/"audit2"),
                                     "ASC_RC_ALLOW_GUI":"1","ASC_RC_ALLOW_EXEC":"1",
                                     "ASC_RC_ALLOW_GAME_AGENT":"0"}):
            agent=Agent()
            with patch("asc_remote_commander.os.name","nt"):
                agent._register_tools()
            self.assertNotIn("game_agent_status",agent.tools)

    def test_public_agent_package_includes_bridge_not_native_dlls(self):
        from distribution import ALLOWED_FILES
        self.assertIn("client/asc_game_agent.py",ALLOWED_FILES)
        self.assertFalse(any(x.lower().endswith((".dll",".onnx",".exe")) for x in ALLOWED_FILES))

    def test_manifest_schema_matches_agent_method_names(self):
        catalogue=json.loads((HERE/"server/tools_manifest.json").read_text("utf8"))
        required=["game_agent_status","game_agent_targets","game_agent_preflight",
                  "game_agent_capture","game_agent_gpu_selftest",
                  "game_agent_open_vocab"]
        chosen={x["name"]:x for x in catalogue if x["name"] in required}
        self.assertEqual(set(required),set(chosen))
        for entry in chosen.values():
            self.assertIn("device",entry["inputSchema"]["required"])
            self.assertFalse(entry["inputSchema"]["additionalProperties"])
        self.assertFalse(chosen["game_agent_capture"]["annotations"]["readOnlyHint"])
        self.assertFalse(chosen["game_agent_gpu_selftest"]["annotations"]["readOnlyHint"])
        self.assertFalse(chosen["game_agent_open_vocab"]["annotations"]["readOnlyHint"])
        self.assertTrue(chosen["game_agent_preflight"]["annotations"]["readOnlyHint"])


if __name__=="__main__":
    unittest.main()
