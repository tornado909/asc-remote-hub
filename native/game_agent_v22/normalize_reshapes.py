"""Fold static ONNX Reshape -1 dimensions for ORT DirectML strict GPU-only."""
from __future__ import annotations

from pathlib import Path
import sys
import onnx
from onnx import helper, numpy_helper, TensorProto


def transform(source: Path, target: Path) -> None:
    m = onnx.shape_inference.infer_shapes(onnx.load(str(source)))
    dimensions = {
        v.name: [d.dim_value for d in v.type.tensor_type.shape.dim]
        for v in [*m.graph.input, *m.graph.output, *m.graph.value_info]
    }
    initial = {v.name: v for v in m.graph.initializer}
    fixed = 0
    unresolved = []
    for node in m.graph.node:
        if node.op_type != "Reshape" or len(node.input) != 2 or node.input[1] not in initial:
            continue
        arg = numpy_helper.to_array(initial[node.input[1]]).ravel()
        if all(int(n) > 0 for n in arg):
            continue
        output_shape = dimensions.get(node.output[0])
        if not output_shape or any(int(n) <= 0 for n in output_shape):
            unresolved.append(node.name)
            continue
        replacement = "asc_static_" + node.name
        m.graph.initializer.append(
            helper.make_tensor(replacement, TensorProto.INT64,
                               [len(output_shape)],
                               [int(n) for n in output_shape])
        )
        node.input[1] = replacement
        fixed += 1
    if unresolved:
        raise RuntimeError("UNRESOLVED_DYNAMIC_RESHAPES: " + ",".join(unresolved))
    if fixed != 11:
        raise RuntimeError(f"UNEXPECTED_RESHAPE_CONVERSION_COUNT:{fixed}")
    m = onnx.shape_inference.infer_shapes(m)
    onnx.checker.check_model(m)
    onnx.save(m, str(target))
    print("DML_STATIC_RESHAPES", {"converted": fixed,
                                 "bytes": target.stat().st_size},flush=True)


if __name__=="__main__":
    if len(sys.argv)!=3:
        raise SystemExit("usage: normalize_reshapes.py <einsum-free.onnx> <output.onnx>")
    transform(Path(sys.argv[1]),Path(sys.argv[2]))
