"""Game-independent Open Vocabulary v22 GPU metadata validation tests.

All inputs synthetic: no capture, no game launch, no installed model/GPU.
"""
import copy
import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"client"))
from asc_game_agent_open_vocab import (
    convert_v22_report, normalize_detection_box, SceneContractError,
    BYTE_METADATA_PER_FRAME, PROVIDER,
)

PROMPTS=["figure","animal","obstacle","tree","icon","entrance","tool","structure"]


def sample_report(*, external=False):
    n=2
    frames=[]
    for i in range(n):
        detections=[
            {"prompt_index":j,"score":.0,"anchor":j*10,
             "box_model_normalized":[0,0,0,0]}
            for j in range(8)
        ]
        detections[0]={"prompt_index":0,"score":.75,"anchor":100+i,
                       "box_model_normalized":[.25,.2,.75,.8]}
        detections[4]={"prompt_index":4,"score":.1,"anchor":200+i,
                       "box_model_normalized":[.8,.25,.95,.35]}
        frames.append({"index":i,"detections":detections,
                       "test_pixel_rgba":[255,128,64,255]})
    return {
        "ok":True,"code":"WINDOW_CAPTURE_SESSION_COMPLETED",
        "version":"22-open-vocabulary-canary",
        "mode":"consented_selected_window_WGC_DirectML_OpenVocab",
        "semantic_provider":"YOLO-World-v2",
        "open_vocabulary":True,"runtime_text_embedding_classes":8,
        "capture_target":"user_selected_window" if external else "self_fixture",
        "consent_origin":"native_local_dialog" if external else "self_fixture_test",
        "capture_authorized_during_session":True,
        "identity_checked_per_frame":True,
        "grant_persisted":False,"grant_valid_after_exit":False,
        "gpu_only":True,"cpu_fallback_disabled":True,
        "screen_recording_saved":False,
        "game_started":False,"host_input_sent":False,
        "full_frames_to_cpu_bytes":0,"tensor_to_cpu_bytes":0,
        "logits_to_cpu_bytes":0,
        "text_embedding_cpu_upload_bytes":16384,
        "model_sessions_created":1,"max_frames_in_flight":1,
        "metadata_to_cpu_bytes":n*BYTE_METADATA_PER_FRAME,
        "diagnostic_pixels_to_cpu_bytes":n*4 if not external else 0,
        "wgc_input_last_width":960,"wgc_input_last_height":540,
        "gpu_tensor_width":1280,"gpu_tensor_height":720,
        "aspect_ratio_preserved":True,
        "target_pid":4567,"target_hwnd":"0xdead",
        "frames_processed":n,"frames":frames,
        "arbitrary_native_extra":"must never leave adapter",
    }


def convert(r=None,**kw):
    inputs={"prompts":PROMPTS,"source_id":"private-window-fixture",
            "source_generation":"per-session-creation-id","external":False}
    if kw.get("external") is True:
        inputs.update({"source_id":"hwnd:0xdead",
                       "source_generation":"win32-start:123456789123456789",
                       "expected_start_ticks":123456789123456789})
    inputs.update(kw)
    return convert_v22_report(r or sample_report(),**inputs)


class UniversalV22ReportTests(unittest.TestCase):
    def test_fixture_returns_uncalibrated_generic_proposal_only(self):
        r=convert()
        self.assertEqual(r["frames_processed"],2)
        self.assertEqual(len(r["scenes"]),2)
        scene=r["scenes"][0]
        self.assertEqual(len(scene["entities"]),1)
        self.assertEqual(scene["entities"][0]["label"],"figure")
        self.assertEqual(scene["entities"][0]["kind"],"object")
        self.assertEqual(scene["entities"][0]["id"],"p0-a100")
        self.assertEqual(scene["entities"][0]["confidence"],.75)
        for got,expected in zip(scene["entities"][0]["box"],[.25,.1,.75,.9]):
            self.assertAlmostEqual(got,expected)
        self.assertFalse(scene["raw_image_included"])
        self.assertFalse(scene["action_authorized"])
        self.assertFalse(scene["input_sent"])
        self.assertFalse(r["semantic_quality_validated"])
        self.assertEqual(PROVIDER.ui_grounding,False)
        self.assertEqual(PROVIDER.ocr,False)
        self.assertNotIn("arbitrary_native_extra",str(r))
        self.assertNotIn("test_pixel_rgba",str(r))

    def test_external_capture_needs_exact_native_dialog_fingerprint(self):
        raw=sample_report(external=True)
        good=convert(raw,external=True,expected_hwnd="0xdead",expected_pid=4567)
        self.assertEqual(good["scenes"][0]["source"]["source_kind"],"window")
        for args in ({"expected_hwnd":None,"expected_pid":4567},
                     {"expected_hwnd":"0xbee","expected_pid":4567},
                     {"expected_hwnd":"0xdead","expected_pid":4568},
                     {"expected_hwnd":"0xdead","expected_pid":True}):
            with self.subTest(args=args),self.assertRaisesRegex(
                SceneContractError,"FINGERPRINT_CHANGED"
            ):
                convert(raw,external=True,**args)
        bad=copy.deepcopy(raw)
        bad["consent_origin"]="self_fixture_test"
        with self.assertRaisesRegex(SceneContractError,"CONSENT"):
            convert(bad,external=True,expected_hwnd="0xdead",expected_pid=4567)

    def test_no_cpu_fallback_frame_pixels_or_unapproved_actions(self):
        for field,value in (
            ("gpu_only",False),("cpu_fallback_disabled",False),
            ("host_input_sent",True),("screen_recording_saved",True),
            ("game_started",True),
        ):
            with self.subTest(field=field):
                report=sample_report()
                report[field]=value
                with self.assertRaisesRegex(SceneContractError,"NO_CPU_OR_INPUT"):
                    convert(report)
        for field in ("full_frames_to_cpu_bytes","tensor_to_cpu_bytes",
                      "logits_to_cpu_bytes"):
            with self.subTest(field=field):
                report=sample_report()
                report[field]=4096
                with self.assertRaisesRegex(SceneContractError,"FRAME_OR_TENSOR"):
                    convert(report)
        for field,value in (("grant_persisted",True),
                            ("identity_checked_per_frame",False),
                            ("capture_authorized_during_session",False)):
            with self.subTest(field=field):
                report=sample_report()
                report[field]=value
                with self.assertRaisesRegex(SceneContractError,"CONSENT"):
                    convert(report)

    def test_invalid_geometry_refused_and_y_unletterboxed(self):
        self.assertEqual(normalize_detection_box([.2,.125,.8,.875]),
                         [.2,0.,.8,1.])
        self.assertEqual(normalize_detection_box([.2,.0,.8,.10]),None)
        self.assertEqual(normalize_detection_box([.2,.90,.8,1.]),None)
        box=normalize_detection_box([0,.0,.5,.25])
        self.assertAlmostEqual(box[1],0)
        self.assertAlmostEqual(box[3],1/6)
        for box in ([.5,.9,.1,.95],[.1,.2,.3,2.0],
                    [.1,float('nan'),.3,.4],[0,True,.4,.6],
                    [0,.3,.4]):
            with self.subTest(box=box),self.assertRaises(SceneContractError):
                normalize_detection_box(box)
        for w,h in ((1024,768),(1000,1000),(0,0),(2560,1600)):
            report=sample_report()
            report["wgc_input_last_width"]=w
            report["wgc_input_last_height"]=h
            with self.subTest(size=(w,h)),self.assertRaisesRegex(
                SceneContractError,"GEOMETRY"
            ):
                convert(report)

    def test_bogus_scores_prompts_and_duplicate_anchor_refused(self):
        for x in (math.nan,math.inf,-.01,1.1,True,".6"):
            report=sample_report()
            report["frames"][0]["detections"][0]["score"]=x
            with self.subTest(value=x),self.assertRaisesRegex(
                SceneContractError,"SCORE_OR_ANCHOR"
            ):
                convert(report)
        bad=sample_report()
        bad["frames"][0]["detections"][0]["prompt_index"]=4
        with self.assertRaisesRegex(SceneContractError,"PROMPT_INDEX"):
            convert(bad)
        bad=sample_report()
        bad["frames"][0]["detections"][1]["anchor"]=6300
        with self.assertRaisesRegex(SceneContractError,"ANCHOR"):
            convert(bad)
        for prompts in (PROMPTS[:-1],
                        PROMPTS[:-1]+[PROMPTS[0].upper()],
                        PROMPTS[:-1]+["bad\nprompt"]):
            with self.assertRaises(SceneContractError):
                convert(prompts=prompts)

    def test_frame_size_and_sequence_consistent(self):
        report=sample_report()
        report["frames"][1]["index"]=0
        with self.assertRaisesRegex(SceneContractError,"SEQUENCE"):
            convert(report)
        report=sample_report()
        report["metadata_to_cpu_bytes"]-=32
        with self.assertRaisesRegex(SceneContractError,"FRAME_OR_COMPACT"):
            convert(report)
        report=sample_report()
        report["text_embedding_cpu_upload_bytes"]=0
        with self.assertRaisesRegex(SceneContractError,"PROVIDER_SESSION"):
            convert(report)

    def test_low_confidence_makes_no_invented_objects(self):
        report=sample_report()
        report["frames"][0]["detections"][0]["score"]=.00001
        report["frames"][1]["detections"][0]["score"]=.00001
        result=convert(report,threshold=.35)
        self.assertEqual([len(x["entities"]) for x in result["scenes"]],[0,0])
        self.assertEqual(len(result["scenes"][0]["uncertainties"]),2)

    def test_fixture_does_not_authorize_external_capture(self):
        with self.assertRaisesRegex(SceneContractError,"CONSENT"):
            convert(sample_report(),external=True,expected_pid=4567,
                    expected_hwnd="0xdead")
        with self.assertRaisesRegex(SceneContractError,"CONSENT"):
            convert(sample_report(external=True))
        bad=sample_report()
        bad["diagnostic_pixels_to_cpu_bytes"]=0
        with self.assertRaisesRegex(SceneContractError,"DIAGNOSTIC"):
            convert(bad)

if __name__=="__main__":
    unittest.main()
