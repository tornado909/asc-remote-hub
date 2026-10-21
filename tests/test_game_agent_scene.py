"""Platform-agnostic ASC Game Agent scene contract regression tests.

These are synthetic frames from *unidentified* applications and games.
No Palworld labels, game-specific classifiers, pixels or GPU use.
"""
import copy
import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"client"))
from asc_game_agent_scene import (
    SCHEMA, FrameIdentity, ProviderCapabilities, SceneContractError,
    TemporalSceneTracker, build_scene
)


def source(n=10,generation="birth:100",width=2560,height=1440,kind="window"):
    return FrameIdentity(source_kind=kind,source_id="window:opaque",
                         source_generation=generation,index=n,width=width,height=height)


def provider(**props):
    settings={
        "name":"generic-runtime",
        "model":"open-vocabulary-vision-v1",
        "open_vocabulary":True,
        "ui_grounding":True,
        "ocr":True,
        "temporal_reasoning":False,
        "gpu_inference":True
    }
    settings.update(props)
    return ProviderCapabilities(**settings)


def observations():
    return {
        "entities":[
            {"id":"a","kind":"unknown","label":"unknown moving shape",
             "box":[.05,.10,.24,.45],"confidence":None},
            {"id":"b","kind":"agent","label":"armored figure",
             "box":[.38,.22,.65,.89],"confidence":.63},
            {"id":"c","kind":"ui","label":"circular inventory icon",
             "box":[.91,.91,.97,.98],"confidence":.82,"interactive":True},
            {"id":"d","kind":"text","label":"text",
             "box":[.7,.03,.98,.1],"confidence":.96,"text":"Меню / Save",
             "interactive":False}
        ],
        "relations":[{"subject":"b","predicate":"near","object":"a"}],
        "summary":"A controllable figure stands near an unidentified moving object.",
        "uncertainties":["Unknown object identity."]
    }


class UniversalSceneTests(unittest.TestCase):
    def test_unknown_world_scene_keeps_open_vocabulary_and_uncertainty(self):
        scene=build_scene(observations(),source=source(),
                          provider=provider(),locally_authorized=True)
        self.assertEqual(scene["schema"],SCHEMA)
        self.assertEqual(scene["entities"][0]["kind"],"unknown")
        self.assertEqual(scene["entities"][0]["label"],"unknown moving shape")
        self.assertIsNone(scene["entities"][0]["confidence"])
        self.assertEqual(scene["entities"][3]["text"],"Меню / Save")
        self.assertFalse(scene["raw_image_included"])
        self.assertFalse(scene["action_authorized"])
        self.assertFalse(scene["input_sent"])
        self.assertEqual(scene["provider"]["open_vocabulary"],True)
        self.assertEqual(scene["source"]["width"],2560)

    def test_unrelated_desktop_ui_uses_exact_same_contract(self):
        ui={
           "entities":[
              {"id":"save","kind":"ui","label":"Save",
               "box":[0.71,0.86,0.82,0.92],"confidence":0.91,
               "interactive":True,"state":"enabled"},
              {"id":"text1","kind":"text","label":"dialog heading",
               "box":[.3,.12,.7,.18],"confidence":.72,"text":"Сохранить документ"}
           ],
           "summary":"A generic settings dialog."
        }
        scene=build_scene(ui,source=source(kind="desktop",width=1280,height=720),
                          provider=provider(),locally_authorized=True)
        self.assertEqual(scene["schema"],SCHEMA)
        self.assertEqual(scene["entities"][0]["label"],"Save")
        self.assertEqual(scene["entities"][1]["text"],"Сохранить документ")
        self.assertEqual(len(scene["relations"]),0)
        self.assertFalse(scene["action_authorized"])

    def test_norm_coordinates_support_different_resolutions(self):
        a=build_scene(observations(),source=source(width=800,height=600),
                      provider=provider(),locally_authorized=True)
        b=build_scene(observations(),source=source(width=3840,height=2160),
                      provider=provider(),locally_authorized=True)
        self.assertEqual(a["entities"][1]["box"],b["entities"][1]["box"])

    def test_no_pixels_scripts_or_extra_fields_ever_escape(self):
        for key,value in (("raw_frame",bytes([0]*20)),
                          ("image_url","https://example.invalid/frame"),
                          ("command","click now"),
                          ("model_path","C:/secret.onnx")):
            with self.subTest(key=key):
                raw=observations()
                raw[key]=value
                with self.assertRaisesRegex(SceneContractError,"unexpected scene fields"):
                    build_scene(raw,source=source(),provider=provider(),
                                locally_authorized=True)
        for key,value in (("pixel_bytes",[0,1,2]),("action","click"),
                          ("raw_tensor",[[10]*5])):
            with self.subTest(key=key):
                raw=observations()
                raw["entities"][0][key]=value
                with self.assertRaisesRegex(SceneContractError,"unexpected entity fields"):
                    build_scene(raw,source=source(),provider=provider(),
                                locally_authorized=True)

    def test_refuses_forged_permission_or_cpu_fallback(self):
        with self.assertRaisesRegex(SceneContractError,"NOT_AUTHORIZED"):
            build_scene(observations(),source=source(),provider=provider(),
                        locally_authorized=False)
        with self.assertRaisesRegex(SceneContractError,"GPU_ONLY"):
            build_scene(observations(),source=source(),
                        provider=provider(gpu_inference=False),
                        locally_authorized=True)

    def test_invalid_coordinates_nonfinite_scores_and_boolean_rejected(self):
        for box in ([0,.1,0,.3],[-.1,.0,.5,.5],[0,0,1.1,.5],
                    [.7,.2,.5,.5],[.1,.8,.9,.2], [.1,.1,float("nan"),.5],
                    [True,.1,.5,.5], [0,0,1]):
            with self.subTest(box=box):
                raw=observations()
                raw["entities"][0]["box"]=box
                with self.assertRaises(SceneContractError):
                    build_scene(raw,source=source(),provider=provider(),
                                locally_authorized=True)
        for score in (float("nan"),float("inf"),-0.01,1.1,True,"0.99"):
            with self.subTest(score=score):
                raw=observations()
                raw["entities"][0]["confidence"]=score
                with self.assertRaises(SceneContractError):
                    build_scene(raw,source=source(),provider=provider(),
                                locally_authorized=True)

    def test_invalid_ids_refs_and_oversized_reports_refused(self):
        raw=observations()
        raw["entities"][1]["id"]="a"
        with self.assertRaisesRegex(SceneContractError,"duplicate"):
            build_scene(raw,source=source(),provider=provider(),locally_authorized=True)
        raw=observations()
        raw["relations"][0]["object"]="does-not-exist"
        with self.assertRaisesRegex(SceneContractError,"unknown"):
            build_scene(raw,source=source(),provider=provider(),locally_authorized=True)
        raw=observations()
        raw["entities"]*=40
        with self.assertRaises(SceneContractError):
            build_scene(raw,source=source(),provider=provider(),locally_authorized=True)

    def test_tracker_preserves_object_without_a_fixed_game_taxonomy(self):
        tracker=TemporalSceneTracker()
        first=tracker.update(build_scene(observations(),source=source(n=1),
                              provider=provider(),locally_authorized=True))
        later=observations()
        later["entities"][0]["box"]=[.06,.10,.25,.45]
        later["entities"][0]["label"]="unidentified flying object"
        result=tracker.update(build_scene(later,source=source(n=2),
                               provider=provider(),locally_authorized=True))
        self.assertEqual(first["entities"][0]["track_id"],result["entities"][0]["track_id"])
        self.assertNotEqual(first["entities"][0]["label"],result["entities"][0]["label"])
        self.assertEqual(result["tracking"]["algorithm"],"generic_normalized_bbox_iou")
        self.assertFalse(result["tracking"]["semantic_identity_verified"])

    def test_tracker_does_not_confuse_new_window_with_previous(self):
        tracker=TemporalSceneTracker()
        initial=tracker.update(build_scene(observations(),source=source(n=7),
                               provider=provider(),locally_authorized=True))
        after=tracker.update(build_scene(observations(),source=source(n=1,generation="birth:101"),
                             provider=provider(),locally_authorized=True))
        self.assertNotEqual(initial["entities"][0]["track_id"],after["entities"][0]["track_id"])

    def test_tracker_rejects_repeated_or_backward_frames(self):
        tracker=TemporalSceneTracker()
        tracker.update(build_scene(observations(),source=source(n=99),
                       provider=provider(),locally_authorized=True))
        for n in (99,98):
            with self.subTest(n=n),self.assertRaisesRegex(SceneContractError,"non-monotonic"):
                tracker.update(build_scene(observations(),source=source(n=n),
                               provider=provider(),locally_authorized=True))

    def test_scene_contract_requires_a_provider_not_native_coco_diagnostics(self):
        # COCO person/bus scalar scores do not carry grounded entities.
        with self.assertRaisesRegex(SceneContractError,"unexpected scene fields"):
            build_scene({"person_confidence":0.6,"bus_confidence":.01},
                        source=source(),provider=provider(),locally_authorized=True)

    def test_universal_scene_module_is_publicly_packaged(self):
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"server"))
        from distribution import ALLOWED_FILES
        self.assertIn("client/asc_game_agent_scene.py",ALLOWED_FILES)
        self.assertIn("asc.game_agent.scene.v1",
                      (Path(__file__).resolve().parents[1]/
                       "docs/GAME_AGENT_UNIVERSAL.md").read_text(encoding="utf8"))

    def test_source_and_provider_shapes_are_strict(self):
        with self.assertRaises(SceneContractError):
            source(width=16)
        with self.assertRaises(SceneContractError):
            provider(gpu_inference=1)
        with self.assertRaises(SceneContractError):
            source(generation="")
        with self.assertRaises(ValueError):
            TemporalSceneTracker(min_iou=math.nan)


if __name__=="__main__":
    unittest.main()
