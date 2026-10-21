"""Generate eight runtime YOLO-World v2 text embeddings on a CUDA GPU.

Only text prompts cross the Python process. NO captured image or pixels are
used or transferred. This generator runs separately from game capture.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import time

MODEL_ID = "openai/clip-vit-base-patch32"
DEFAULT_LABELS = [
    "person", "creature", "rock", "tree", "button", "door", "object", "building"
]


def generate(prompts: list[str], output_dir: Path) -> None:
    if len(prompts) != 8 or any(type(x) is not str or
                               not (1 <= len(x.strip()) <= 100) or
                               any(ord(c) < 32 for c in x)
                               for x in prompts):
        raise ValueError("Eight nonempty, bounded text prompts are required")
    if len({x.strip().casefold() for x in prompts}) != 8:
        raise ValueError("Text prompts must be unique")
    # Delayed imports: normal import/static tests never trigger downloads/GPU.
    import torch
    import torch.nn.functional as F
    from transformers import AutoTokenizer,CLIPTextModelWithProjection

    if not torch.cuda.is_available():
        raise RuntimeError("GPU_REQUIRED_FOR_CLIP_TEXT_EMBEDDINGS")
    output_dir.mkdir(parents=True,exist_ok=True)
    destination=output_dir/"yoloworld_clip8_features.bin"
    metadata_file=output_dir/"yoloworld_clip8_features.json"
    if destination.exists() or metadata_file.exists():
        raise RuntimeError("REFUSE_OVERWRITE_EXISTING_PROMPT_EMBEDDINGS")
    start=time.monotonic()
    tokenizer=AutoTokenizer.from_pretrained(MODEL_ID)
    encoder=CLIPTextModelWithProjection.from_pretrained(
        MODEL_ID,dtype=torch.float16).to("cuda").eval()
    arguments=tokenizer(prompts,padding=True,truncation=True,return_tensors="pt")
    arguments={key:value.to("cuda") for key,value in arguments.items()}
    with torch.inference_mode():
        features=encoder(**arguments).text_embeds.float()
        features=F.normalize(features,p=2,dim=-1)
        binary=features.cpu().numpy().astype("<f4").tobytes()
    if len(binary)!=8*512*4:
        raise RuntimeError("INVALID_TEXT_FEATURE_TENSOR")
    destination.write_bytes(binary)
    meta={
        "model":MODEL_ID,
        "prompts":[s.strip() for s in prompts],
        "tensor_shape":[1,8,512],
        "dtype":"float32",
        "origin":"GPU-only CLIPTextModelWithProjection",
        "gpu":torch.cuda.get_device_name(0),
        "sha256":hashlib.sha256(binary).hexdigest(),
        "bytes":len(binary),
        "seconds":round(time.monotonic()-start,2),
    }
    metadata_file.write_text(json.dumps(meta,ensure_ascii=False,indent=2)+"\n",
                             encoding="utf-8")
    print("ASC_V22_TEXT_ONLY_GPU_EMBEDDINGS_READY",json.dumps(meta,ensure_ascii=False))


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--prompts-json",type=Path)
    parser.add_argument("--output-dir",type=Path,required=True)
    args=parser.parse_args()
    prompts=DEFAULT_LABELS if args.prompts_json is None else json.loads(
        args.prompts_json.read_text(encoding="utf-8")
    )
    if not isinstance(prompts,list):
        raise ValueError("Prompt document must contain a JSON list")
    generate(prompts,args.output_dir)
