"""Reproduce the strictly GPU-compatible YOLO-World v2 ONNX graph.

Offline, *not* model inference. Third-party weights are downloaded and
licensed separately; this script never downloads, trains or distributes them.

Requirements: Python 3.10+, onnx==1.17.0, onnxsim==0.4.36 and their
conversion-only dependencies. Runtime GPU-only inference uses DirectML.
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import tempfile

import onnx
import onnxsim

from rewrite_einsum import transform as rewrite_einsum
from normalize_reshapes import transform as normalize_reshapes

SOURCE_SHA = "381ced485b23ed8f06de3e82bb2745e1420c181c64f0a176784c34a959d550a1"
RESULT_SHA = "45d112de3b14d694a675d150da25afcc1c6efbfe9f42fdf5272002d1dbc88cc1"
RESULT_NAME = "yolov8s-worldv2-static8-reshape-normalized.onnx"


def sha_file(path: Path) -> str:
    hash = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            hash.update(chunk)
    return hash.hexdigest()


def convert(source: Path, destination: Path) -> None:
    if not source.is_file() or source.is_symlink() or source.suffix.lower() != ".onnx":
        raise RuntimeError("PINNED_ONNX_SOURCE_REQUIRED")
    if sha_file(source) != SOURCE_SHA:
        raise RuntimeError("ONNX_SOURCE_SHA256_NOT_THE_VALIDATED_MODEL")
    if destination.exists() or destination.name != RESULT_NAME:
        raise RuntimeError("REFUSE_OVERWRITE_OR_UNEXPECTED_RESULT_NAME")
    destination.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="asc-v22-build-",dir=destination.parent) as workspace:
        temporary=Path(workspace)
        model=onnx.load(str(source))
        simplified,verified=onnxsim.simplify(
            model,
            check_n=0,
            overwrite_input_shapes={"images":[1,3,480,640],
                                    "txt_feats":[1,8,512]},
            dynamic_input_shape=False,
        )
        onnx.checker.check_model(simplified)
        intermediate=temporary/"static.onnx"
        onnx.save(simplified,str(intermediate))
        einsum=temporary/"einsum-free.onnx"
        rewrite_einsum(intermediate,einsum)
        ready=temporary/RESULT_NAME
        normalize_reshapes(einsum,ready)
        result_hash=sha_file(ready)
        if result_hash!=RESULT_SHA:
            raise RuntimeError("GPU_ONLY_RESULT_DIFFERS_FROM_VALIDATED_SHA")
        ready.replace(destination)
    print("ASC_V22_GPU_ONLY_ONNX_REPRODUCED",{
        "output_bytes":destination.stat().st_size,
        "sha256":RESULT_SHA,
        "batch":1,"input_shape":[1,3,480,640],
        "runtime_prompt_classes":8,"embedding_dim":512,
        "training_per_game_required":False,
        "runtime_cpu_fallback":False
    },flush=True)


if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("source",type=Path)
    p.add_argument("output",type=Path)
    args=p.parse_args()
    convert(args.source,args.output)
