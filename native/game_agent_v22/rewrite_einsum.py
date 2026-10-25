"""Replace YOLO-World ONNX Einsum contractions with equivalent GPU MatMul.

Only offline graph transformation, not model inference. Fixes input tensor
shapes to 1x3x480x640 and text embeddings to 1x8x512, while leaving the
*embedding values* as a runtime input. No game names or class weights baked.
"""
from __future__ import annotations
from pathlib import Path
import sys
from collections import Counter
import onnx
from onnx import TensorProto, helper


def transform(src: Path, dst: Path) -> None:
    model = onnx.load(str(src))
    inspected = onnx.shape_inference.infer_shapes(model)
    dimensions = {}
    for val in list(inspected.graph.input) + list(inspected.graph.value_info):
        dimensions[val.name] = [d.dim_value for d in val.type.tensor_type.shape.dim]
    graph = model.graph
    replacement = []
    converted = 0

    def shape_tensor(prefix: str, dims: list[int]) -> str:
        name = prefix + "_shape"
        graph.initializer.append(helper.make_tensor(name, TensorProto.INT64,
                                                    [len(dims)], dims))
        return name

    for n in graph.node:
        if n.op_type != "Einsum":
            replacement.append(n)
            continue
        eq = next(a.s.decode() for a in n.attribute if a.name == "equation")
        assert len(n.input) == 2 and len(n.output) == 1
        x, y = n.input
        xd, yd = dimensions[x], dimensions[y]
        prefix = "asc_v22_" + n.name
        finished = n.output[0]
        if eq == "bmchw,bnmc->bmhwn":
            b, m, c, h, w = xd
            b2, nclass, m2, c2 = yd
            assert b == b2 and m == m2 and c == c2
            tx = prefix + "_t_input"
            vx = prefix + "_flat_input"
            ty = prefix + "_t_feats"
            result = prefix + "_matmul"
            replacement.extend([
                helper.make_node("Transpose", [x], [tx], perm=[0,1,3,4,2],
                                 name=prefix+"_image_transpose"),
                helper.make_node("Reshape",
                                 [tx, shape_tensor(prefix+"_lhs", [b,m,h*w,c])],
                                 [vx], name=prefix+"_flat"),
                helper.make_node("Transpose", [y], [ty], perm=[0,2,3,1],
                                 name=prefix+"_embeddings_transpose"),
                helper.make_node("MatMul", [vx,ty], [result],
                                 name=prefix+"_gpu_matmul"),
                helper.make_node("Reshape",
                                 [result,shape_tensor(prefix+"_out", [b,m,h,w,nclass])],
                                 [finished], name=prefix+"_restore")
            ])
        elif eq == "bchw,bkc->bkhw":
            b,c,h,w = xd
            b2,k,c2=yd
            assert b==b2 and c==c2
            flat=prefix+"_flatten"
            result=prefix+"_matmul"
            replacement.extend([
                helper.make_node("Reshape",
                                 [x,shape_tensor(prefix+"_input",[b,c,h*w])],
                                 [flat],name=prefix+"_flatten_node"),
                helper.make_node("MatMul",[y,flat],[result],
                                 name=prefix+"_gpu_matmul"),
                helper.make_node("Reshape",
                                 [result,shape_tensor(prefix+"_out",[b,k,h,w])],
                                 [finished],name=prefix+"_restore")
            ])
        else:
            raise RuntimeError("Unexpected Einsum equation " + eq)
        converted += 1

    assert converted == 7, converted
    graph.ClearField("node")
    graph.node.extend(replacement)
    onnx.checker.check_model(model)
    onnx.shape_inference.infer_shapes(model)
    onnx.save(model,str(dst))
    print("ONNX_EINSUM_REWRITE_PASS",{
        "converted": converted,"remaining":Counter(x.op_type for x in graph.node).get("Einsum",0),
        "nodes":len(graph.node),"bytes":dst.stat().st_size,
        "input":[(x.name,[d.dim_value for d in x.type.tensor_type.shape.dim]) for x in graph.input]
    },flush=True)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: rewrite_einsum.py <static-input.onnx> <output.onnx>")
    transform(Path(sys.argv[1]),Path(sys.argv[2]))
