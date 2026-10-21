"""Universal, game-independent scene metadata contract and temporal tracker.

This module NEVER captures pixels, loads models, sends input, or identifies
a named game. A separate trusted, consent-checked GPU vision provider must
produce the observations. Native v19 COCO confidence diagnostics do NOT meet
this semantic perception contract.

Input observations are untrusted model output. Only allowlisted metadata can
leave the provider; raw frames, pixel tensors, scripts and commands cannot.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any, Mapping

SCHEMA = "asc.game_agent.scene.v1"
KINDS = frozenset({"object", "agent", "terrain", "ui", "text", "effect", "unknown"})
MAX_OBSERVATIONS = 128
MAX_RELATIONS = 128


class SceneContractError(ValueError):
    """Untrusted Vision provider output did not satisfy the scene contract."""


def _str(value: object, *, name: str, limit: int = 192,
         empty: bool = False) -> str:
    if not isinstance(value, str):
        raise SceneContractError(name + " must be a string")
    trimmed = value.strip()
    if not empty and not trimmed:
        raise SceneContractError(name + " is empty")
    if len(trimmed) > limit or any(ord(ch) < 32 for ch in trimmed):
        raise SceneContractError(name + " contains oversized/control text")
    return trimmed


def _int(value: object, *, name: str, lower: int = 0, upper: int = 2**63-1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
        raise SceneContractError(name + " invalid integer")
    return value


def _conf(value: object, *, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise SceneContractError(name + " invalid score")
    score = float(value)
    if not math.isfinite(score) or not 0.0 <= score <= 1.0:
        raise SceneContractError(name + " outside 0..1")
    return score


def _box(value: object) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise SceneContractError("box must have four normalized coordinates")
    x1, y1, x2, y2 = (_conf(n, name="box coordinate") for n in value)
    if any(n is None for n in (x1, y1, x2, y2)) or not x1 < x2 or not y1 < y2:
        raise SceneContractError("box must have positive normalized dimensions")
    return [x1, y1, x2, y2]


def _keys(value: Mapping[str, Any], allowed: set[str], label: str) -> None:
    if value.keys() - allowed:
        raise SceneContractError("unexpected " + label + " fields")


@dataclass(frozen=True)
class FrameIdentity:
    # No Win32/PID/Unreal specifics in the universal core.
    source_kind: str
    source_id: str
    source_generation: str
    index: int
    width: int
    height: int

    def __post_init__(self):
        _str(self.source_kind, name="source_kind", limit=40)
        _str(self.source_id, name="source_id")
        _str(self.source_generation, name="source_generation")
        _int(self.index, name="frame index")
        _int(self.width, name="width", lower=64, upper=8192)
        _int(self.height, name="height", lower=64, upper=8192)


@dataclass(frozen=True)
class ProviderCapabilities:
    name: str
    model: str
    open_vocabulary: bool
    ui_grounding: bool
    ocr: bool
    temporal_reasoning: bool
    gpu_inference: bool

    def __post_init__(self):
        _str(self.name, name="provider name")
        _str(self.model, name="model identifier")
        for key in ("open_vocabulary", "ui_grounding", "ocr",
                    "temporal_reasoning", "gpu_inference"):
            if type(getattr(self, key)) is not bool:
                raise SceneContractError("invalid provider capability: " + key)


def build_scene(
    observation: Mapping[str, Any], *,
    source: FrameIdentity,
    provider: ProviderCapabilities,
    locally_authorized: bool
) -> dict[str, Any]:
    """Normalize dynamic model output. No game-specific labels or logic.

    Caller must validate source fingerprint and ephemeral native consent.
    Capability objects must be supplied by a trusted local provider registry,
    *never* by an untrusted vision model or a remote prompt.
    """
    if type(locally_authorized) is not bool or not locally_authorized:
        raise SceneContractError("SCENE_CAPTURE_NOT_AUTHORIZED")
    if not provider.gpu_inference:
        raise SceneContractError("SCENE_GPU_ONLY_REQUIRED")
    if not isinstance(observation, Mapping):
        raise SceneContractError("scene observation must be an object")
    _keys(observation, {"entities", "relations", "summary", "uncertainties"}, "scene")
    raw_entities = observation.get("entities")
    if not isinstance(raw_entities, list) or len(raw_entities) > MAX_OBSERVATIONS:
        raise SceneContractError("invalid scene entities")
    objects: list[dict[str, Any]] = []
    ids: set[str] = set()
    for index, raw in enumerate(raw_entities):
        if not isinstance(raw, Mapping):
            raise SceneContractError("invalid entity object")
        _keys(raw, {"id", "kind", "label", "box", "confidence",
                    "text", "interactive", "state"}, "entity")
        identifier = _str(raw.get("id"), name="entity id", limit=80)
        if identifier in ids:
            raise SceneContractError("duplicate entity identifier")
        ids.add(identifier)
        kind = _str(raw.get("kind", "unknown"), name="entity kind", limit=20)
        if kind not in KINDS:
            raise SceneContractError("unrecognized generic entity kind")
        label = _str(raw.get("label", "unknown"), name="entity label", limit=256)
        interactive = raw.get("interactive", None)
        if interactive is not None and type(interactive) is not bool:
            raise SceneContractError("interactive must be bool or null")
        obj = {
            "id": identifier,
            "kind": kind,
            "label": label,
            "box": _box(raw.get("box")),
            "confidence": _conf(raw.get("confidence"), name="entity confidence"),
            "interactive": interactive
        }
        if "text" in raw:
            obj["text"] = _str(raw["text"], name="OCR text", limit=512, empty=True)
        if "state" in raw:
            obj["state"] = _str(raw["state"], name="state", limit=256, empty=True)
        objects.append(obj)

    raw_relations = observation.get("relations", [])
    if not isinstance(raw_relations, list) or len(raw_relations) > MAX_RELATIONS:
        raise SceneContractError("invalid scene relations")
    relations = []
    for raw in raw_relations:
        if not isinstance(raw, Mapping):
            raise SceneContractError("invalid relation")
        _keys(raw, {"subject", "predicate", "object"}, "relation")
        subject = _str(raw.get("subject"), name="relation subject", limit=80)
        target = _str(raw.get("object"), name="relation object", limit=80)
        if subject not in ids or target not in ids:
            raise SceneContractError("relation references unknown entities")
        relations.append({
            "subject": subject, "predicate": _str(raw.get("predicate"),
                                                  name="relation predicate",
                                                  limit=100),
            "object": target
        })

    uncertainty = observation.get("uncertainties", [])
    if not isinstance(uncertainty, list) or len(uncertainty) > 32:
        raise SceneContractError("invalid uncertainty list")
    notes = [_str(item, name="uncertainty", limit=256) for item in uncertainty]
    return {
        "schema": SCHEMA,
        "source": asdict(source),
        "provider": asdict(provider),
        "entities": objects,
        "relations": relations,
        "summary": _str(observation.get("summary", ""), name="scene summary",
                        limit=1024, empty=True),
        "uncertainties": notes,
        "raw_image_included": False,
        "input_sent": False,
        # A semantic observation is NEVER itself authority to send input.
        "action_authorized": False,
    }


def _iou(first: list[float], second: list[float]) -> float:
    x1 = max(first[0], second[0])
    y1 = max(first[1], second[1])
    x2 = min(first[2], second[2])
    y2 = min(first[3], second[3])
    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    a = (first[2] - first[0]) * (first[3] - first[1])
    b = (second[2] - second[0]) * (second[3] - second[1])
    return intersection / (a+b-intersection)


class TemporalSceneTracker:
    """CPU-light *metadata-only* tracker, no pixels and no game taxonomy.

    This is intentionally a short-lived, per-target tracker, not a global
    person/device identity tracker. It resets when a window is replaced.
    """
    def __init__(self, *, min_iou: float = 0.35, max_gap: int = 3):
        if not isinstance(min_iou, (float, int)) or isinstance(min_iou, bool) \
                or not math.isfinite(min_iou) or not 0 < min_iou <= 1:
            raise ValueError("invalid tracker IoU")
        self.min_iou = float(min_iou)
        self.max_gap = _int(max_gap, name="max_gap", lower=1, upper=30)
        self._source: tuple[str, str, str] | None = None
        self._seq = -1
        self._ids = 0
        self._tracks: dict[str, tuple[str, list[float], int]] = {}

    def update(self, scene: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(scene, Mapping) or scene.get("schema") != SCHEMA:
            raise SceneContractError("tracker requires a validated scene")
        # build_scene has already applied size and content limits.
        source = scene["source"]
        identity = (source["source_kind"], source["source_id"],
                    source["source_generation"])
        frame = source["index"]
        if identity != self._source:
            self._tracks.clear()
            self._source = identity
            self._seq = -1
        if frame <= self._seq:
            raise SceneContractError("non-monotonic frame index")
        self._seq = frame
        previous = dict(self._tracks)
        unmatched = set(previous)
        output = []
        for obj in scene["entities"]:
            best = None
            best_score = self.min_iou
            for tid in sorted(unmatched):
                kind, old_box, last_frame = previous[tid]
                if kind != obj["kind"] or frame-last_frame > self.max_gap:
                    continue
                overlap = _iou(obj["box"], old_box)
                if overlap >= best_score:
                    best, best_score = tid, overlap
            if best is None:
                self._ids += 1
                best = "track-" + str(self._ids)
            else:
                unmatched.remove(best)
            self._tracks[best] = (obj["kind"], obj["box"], frame)
            output.append({**obj, "track_id": best})
        # Keep recent invisible tracks to survive a few occluded frames.
        self._tracks = {id: info for id, info in self._tracks.items()
                        if frame-info[2] <= self.max_gap}
        result = dict(scene)
        result["entities"] = output
        result["tracking"] = {
            "algorithm": "generic_normalized_bbox_iou",
            "semantic_identity_verified": False,
            "pixel_processing": False
        }
        return result
