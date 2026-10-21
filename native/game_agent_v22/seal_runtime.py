"""Seal local ASC Game Agent v22 research runtime, without redistribution.

The GitHub repository contains only project source. Third-party binaries and
weights stay on the user's own test computer. This writes local integrity
hashes for accidental corruption detection; it is NOT a code signature.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

RUNTIME = "windows-universal-vision-v22-runtime"
FILES = (
    "bin/asc_consent_wgc_vision.exe",
    "bin/onnxruntime.dll",
    "bin/DirectML.dll",
    "bin/onnxruntime_providers_shared.dll",
    "models/yolov8s-worldv2-static8-reshape-normalized.onnx",
    "models/yoloworld_clip8_features.bin",
    "models/yoloworld_clip8_features.json",
    "Run-UniversalVision.ps1",
)
MODEL_SHA = "45d112de3b14d694a675d150da25afcc1c6efbfe9f42fdf5272002d1dbc88cc1"


def hash_file(path: Path) -> str:
    d = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            d.update(chunk)
    return d.hexdigest()


def seal(root: Path) -> Path:
    root = root.resolve(strict=True)
    if root.name != RUNTIME:
        raise RuntimeError("REFUSE_UNEXPECTED_RUNTIME_PATH")
    if (root / "SHA256SUMS.json").exists():
        raise RuntimeError("MANIFEST_ALREADY_PRESENT: do not overwrite a sealed build")
    actual = set()
    for item in root.rglob("*"):
        if item.is_symlink():
            raise RuntimeError("REFUSE_SYMLINK")
        if item.is_file():
            actual.add(item.relative_to(root).as_posix())
    if actual != set(FILES):
        raise RuntimeError("UNEXPECTED_RUNTIME_FILES: missing or extra files")
    if hash_file(root / FILES[4]) != MODEL_SHA:
        raise RuntimeError("ONNX_MODEL_NOT_VALIDATED_SHA")
    metadata = json.loads((root / FILES[6]).read_text(encoding="utf-8"))
    prompts = metadata.get("prompts", [])
    if (metadata.get("sha256") != hash_file(root / FILES[5]) or
        metadata.get("tensor_shape") != [1,8,512] or
        metadata.get("model") != "openai/clip-vit-base-patch32" or
        metadata.get("dtype") != "float32" or
        len(prompts) != 8 or
        any(type(s) is not str or not 1<=len(s.strip())<=100 for s in prompts) or
        len({s.strip().casefold() for s in prompts}) != 8):
        raise RuntimeError("INVALID_DYNAMIC_GPU_TEXT_EMBEDDING_METADATA")
    manifest = {
        "schema": 2,
        "release": "asc-game-agent-universal-vision-v22-private-canary",
        "third_party_weights_bundled_publicly": False,
        "local_native_consent_required": True,
        "project_source": "tornado909/asc-remote-hub",
        "files": [
            {"path": name, "bytes": (root/name).stat().st_size,
             "sha256": hash_file(root/name)}
            for name in FILES
        ],
    }
    target = root/"SHA256SUMS.json"
    target.write_text(json.dumps(manifest, ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    return target


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: seal_runtime.py <private-runtime-dir>")
    path=seal(Path(sys.argv[1]))
    print("ASC_V22_PRIVATE_RUNTIME_SEALED",path,len(FILES))
