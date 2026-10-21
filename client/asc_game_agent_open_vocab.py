"""Convert consented v22 open-vocabulary GPU metadata to game-agnostic scenes.

Offline pure Python. No pixel readback, model execution, Win32 calls, AI
network calls, or actions. A separately authenticated native capture adapter
must validate HWND/PID/process-creation and obtain native local consent.
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

from asc_game_agent_scene import (
    FrameIdentity, ProviderCapabilities, SceneContractError, build_scene,
)

MODEL_SIZE = (640, 480)
MODEL_IMAGE_BAND = (60 / 480, 420 / 480)  # Native v19/v22 16:9 -> 4:3 pad.
EMBEDDING_CLASSES = 8
MAX_FRAMES = 32
BYTE_METADATA_PER_FRAME = 256
BYTE_PROMPT_FEATURES = 8 * 512 * 4
PROVIDER = ProviderCapabilities(
    name="local-wgc-directml-open-vocabulary",
    model="YOLO-World-v2",
    open_vocabulary=True,
    ui_grounding=False,
    ocr=False,
    temporal_reasoning=False,
    gpu_inference=True,
)


def _require(flag: bool, why: str) -> None:
    if not flag:
        raise SceneContractError("V22_" + why)


def _valid_int(value: Any, *, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _valid_score(value: Any) -> bool:
    return (type(value) in (int, float) and
            math.isfinite(float(value)) and 0.0 <= value <= 1.0)


def normalize_detection_box(value: Any) -> list[float] | None:
    """Map model 640x480 16:9 letterboxed box into original 16:9 viewport.

    Padding is 60 model pixels above and below a 640x360 GPU-resized scene.
    Boxes wholly outside the image are discarded. Crossing boxes are clipped.
    This function MUST NOT be used for arbitrary aspect ratios.
    """
    _require(isinstance(value, (list, tuple)) and len(value) == 4, "BOX_FORMAT")
    _require(all(_valid_score(v) for v in value), "BOX_COORD_INVALID")
    x1, y1, x2, y2 = [float(v) for v in value]
    _require(x1 <= x2 and y1 <= y2, "BOX_ORDER_INVALID")
    ymin, ymax = MODEL_IMAGE_BAND
    if y2 <= ymin or y1 >= ymax or x1 >= x2:
        return None
    y1 = min(1.0, max(0.0, (y1 - ymin) / (ymax - ymin)))
    y2 = min(1.0, max(0.0, (y2 - ymin) / (ymax - ymin)))
    if y2 <= y1:
        return None
    return [x1, y1, x2, y2]


def _prompts(labels: Sequence[str]) -> list[str]:
    _require(isinstance(labels, (list, tuple)) and len(labels) == EMBEDDING_CLASSES,
             "EXACTLY_8_EMBEDDINGS_REQUIRED")
    clean = []
    seen = set()
    for label in labels:
        _require(isinstance(label, str) and 1 <= len(label.strip()) <= 100 and
                 all(ord(ch) >= 32 for ch in label), "PROMPT_INVALID")
        l = label.strip()
        _require(l.casefold() not in seen, "DUPLICATE_PROMPT")
        seen.add(l.casefold())
        clean.append(l)
    return clean


def convert_v22_report(
    report: Mapping[str, Any], *,
    prompts: Sequence[str],
    source_id: str,
    source_generation: str,
    external: bool,
    expected_pid: int | None = None,
    expected_hwnd: str | None = None,
    expected_start_ticks: int | None = None,
    threshold: float = 0.25,
) -> dict[str, Any]:
    """Trust only allowlisted, cross-validated native capture metadata.

    Caller MUST obtain prompts from the hash-pinned locally stored metadata,
    not from the model response or a free-form remote tool call. On external
    capture, expected HWND/PID/generation MUST be independently pinned from
    native window selection. Consent cannot be inferred from user text.
    """
    _require(type(external) is bool, "SCOPE_INVALID")
    labels = _prompts(prompts)
    _require(_valid_score(threshold) and threshold >= .01, "THRESHOLD_INVALID")
    _require(isinstance(report, Mapping), "REPORT_NOT_OBJECT")
    _require(report.get("ok") is True and
             report.get("version") == "22-open-vocabulary-canary" and
             report.get("code") == "WINDOW_CAPTURE_SESSION_COMPLETED",
             "REPORT_VERSION_INVALID")
    _require(report.get("semantic_provider") == "YOLO-World-v2" and
             report.get("open_vocabulary") is True and
             report.get("runtime_text_embedding_classes") == EMBEDDING_CLASSES,
             "PROVIDER_MISMATCH")
    _require(report.get("mode") ==
             "consented_selected_window_WGC_DirectML_OpenVocab",
             "CAPTURE_BACKEND_MISMATCH")

    scope = "user_selected_window" if external else "self_fixture"
    consent = "native_local_dialog" if external else "self_fixture_test"
    _require(report.get("capture_target") == scope and
             report.get("consent_origin") == consent and
             report.get("capture_authorized_during_session") is True and
             report.get("identity_checked_per_frame") is True and
             report.get("grant_persisted") is False and
             report.get("grant_valid_after_exit") is False,
             "CAPTURE_CONSENT_OR_IDENTITY_INVALID")
    _require(report.get("gpu_only") is True and
             report.get("cpu_fallback_disabled") is True and
             report.get("screen_recording_saved") is False and
             report.get("game_started") is False and
             report.get("host_input_sent") is False,
             "NO_CPU_OR_INPUT_CONTRACT_FAILED")
    _require(report.get("full_frames_to_cpu_bytes") == 0 and
             report.get("tensor_to_cpu_bytes") == 0 and
             report.get("logits_to_cpu_bytes") == 0,
             "FULL_FRAME_OR_TENSOR_LEAK")
    _require(report.get("text_embedding_cpu_upload_bytes") ==
             BYTE_PROMPT_FEATURES and
             report.get("model_sessions_created") == 1 and
             report.get("max_frames_in_flight") == 1,
             "GPU_PROVIDER_SESSION_INVALID")

    frames = report.get("frames")
    n = report.get("frames_processed")
    _require(_valid_int(n, minimum=1, maximum=MAX_FRAMES) and
             isinstance(frames, list) and len(frames) == n and
             report.get("metadata_to_cpu_bytes") == n * BYTE_METADATA_PER_FRAME,
             "FRAME_OR_COMPACT_METADATA_INVALID")
    _require(report.get("diagnostic_pixels_to_cpu_bytes") ==
             (n * 4 if not external else 0),
             "UNEXPECTED_DIAGNOSTIC_PIXELS")

    width = report.get("wgc_input_last_width")
    height = report.get("wgc_input_last_height")
    _require(_valid_int(width, minimum=64, maximum=8192) and
             _valid_int(height, minimum=64, maximum=8192) and
             abs(width / height - 16 / 9) < .025 and
             report.get("aspect_ratio_preserved") is True and
             report.get("gpu_tensor_width") == 1280 and
             report.get("gpu_tensor_height") == 720,
             "UNSUPPORTED_INPUT_GEOMETRY")

    pid = report.get("target_pid")
    hwnd = report.get("target_hwnd")
    _require(_valid_int(pid, minimum=1, maximum=2**31 - 1) and
             isinstance(hwnd, str) and hwnd.lower().startswith("0x"),
             "SOURCE_IDENTITY_INVALID")
    if external:
        _require(expected_pid is not None and expected_hwnd is not None and
                 type(expected_pid) is int and pid == expected_pid and
                 hwnd.lower() == expected_hwnd.lower() and
                 _valid_int(expected_start_ticks, minimum=1, maximum=2**63 - 1) and
                 source_id == "hwnd:" + expected_hwnd.lower() and
                 source_generation == "win32-start:" + str(expected_start_ticks),
                 "SOURCE_FINGERPRINT_CHANGED")
    # The model supplies no semantic claims about the video window's game.
    # Every emitted entity is a single query-matched proposal with unknown
    # interactive status; VLM, OCR and UI-grounding remain unavailable.
    scenes: list[dict[str, Any]] = []
    for index, frame in enumerate(frames):
        _require(isinstance(frame, Mapping) and frame.get("index") == index,
                 "FRAME_SEQUENCE_INVALID")
        entries = frame.get("detections")
        _require(isinstance(entries, list) and
                 len(entries) == EMBEDDING_CLASSES, "DETECTION_COUNT_INVALID")
        objects = []
        ids = set()
        for det in entries:
            _require(isinstance(det, Mapping), "DETECTION_NOT_OBJECT")
            prompt_idx = det.get("prompt_index")
            score = det.get("score")
            anchor = det.get("anchor")
            _require(_valid_int(prompt_idx, minimum=0,
                                maximum=EMBEDDING_CLASSES-1) and
                     prompt_idx not in ids, "PROMPT_INDEX_INVALID")
            ids.add(prompt_idx)
            _require(_valid_score(score) and
                     _valid_int(anchor, minimum=0, maximum=6299),
                     "DETECTION_SCORE_OR_ANCHOR_INVALID")
            viewport_box = normalize_detection_box(det.get("box_model_normalized"))
            if score < threshold or viewport_box is None:
                continue
            objects.append({
                "id": f"p{prompt_idx}-a{anchor}",
                "kind": "object",
                "label": labels[prompt_idx],
                "confidence": float(score),  # Raw detection score, not calibrated.
                "box": viewport_box,
                "interactive": None,
            })
        _require(len(ids) == EMBEDDING_CLASSES, "MISSING_PROMPT_INDEX")
        identity = FrameIdentity(
            source_kind="window" if external else "fixture",
            source_id=source_id,
            source_generation=source_generation,
            index=index,
            width=width,
            height=height,
        )
        scenes.append(build_scene({
            "entities": objects,
            "relations": [],
            "summary": "",
            "uncertainties": [
                "Detector confidence scores are not calibrated probabilities.",
                "One proposal per text prompt; OCR, UI grounding and VLM are unavailable.",
            ],
        }, source=identity, provider=PROVIDER, locally_authorized=True))
    return {
        "schema": "asc.game_agent.perception_batch.v1",
        "provider": "YOLO-World-v2",
        "frames_processed": n,
        "prompts": labels,
        "threshold": float(threshold),
        "scenes": scenes,
        "pixels_or_actions_emitted": False,
        "native_report_verified": True,
        "semantic_quality_validated": False,
    }
