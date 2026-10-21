"""Pure offline public-source and fail-closed v22 native canary tests.

No ONNX, game capture, DLL loading, screenshots, code signing or GPU needed.
"""
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
NATIVE=ROOT/"native"/"game_agent_v22"


class UniversalNativeCanaryContractTests(unittest.TestCase):
    def test_required_source_only_no_native_binaries_in_public_archive(self):
        for name in (
            "gpu_open_vocab_post.hpp","gpu_open_vocab_session.hpp",
            "patch_v19.py","seal_runtime.py","Run-UniversalVision.ps1",
            "rewrite_einsum.py","normalize_reshapes.py",
            "prepare_model.py","generate_text_embeddings.py"
        ):
            self.assertTrue((NATIVE/name).is_file(),name)
        sys.path.insert(0,str(ROOT/"server"))
        from distribution import ALLOWED_FILES
        self.assertIn("client/asc_game_agent_open_vocab.py",ALLOWED_FILES)
        self.assertFalse(any(f.endswith((".onnx",".dll",".exe",".safetensors"))
                             for f in ALLOWED_FILES))
        self.assertFalse(any(x.startswith("native/") for x in ALLOWED_FILES))

    def test_native_is_gpu_only_compact_and_physically_consented(self):
        src=(NATIVE/"gpu_open_vocab_session.hpp").read_text(encoding="utf-8")
        post=(NATIVE/"gpu_open_vocab_post.hpp").read_text(encoding="utf-8")
        wrapper=(NATIVE/"Run-UniversalVision.ps1").read_text(encoding="utf-8")
        self.assertIn("kOrtSessionOptionsDisableCPUEPFallback",src)
        self.assertIn('options.AddConfigEntry(kOrtSessionOptionsDisableCPUEPFallback,"1")',src)
        self.assertIn("CreateGPUAllocationFromD3DResource(tensor.Get()",src)
        self.assertIn("CreateGPUAllocationFromD3DResource(features.Get()",src)
        self.assertIn("ASC_V22_METADATA_BYTES=UINT64(ASC_V22_CLASSES)*32ull",post)
        self.assertIn("commands_->CopyBufferRegion(readback_.Get()",post)
        self.assertIn("commands_->Dispatch(ASC_V22_CLASSES,1,1)",post)
        self.assertIn("$policy.game_agent.capture -ne $true",wrapper)
        self.assertIn("local Yes/No",wrapper)
        self.assertIn("native_local_dialog",wrapper)
        self.assertIn("SHA256SUMS.json",wrapper)
        self.assertIn("ASC_V22_UNSEALED_RUNTIME_FILE",wrapper)
        self.assertIn("Remove-Item -LiteralPath $report",wrapper)
        self.assertNotIn("SendInput(",src+post)
        self.assertNotIn("auto-approved",src+post)

    def test_graph_transformations_keep_runtime_prompt_embeddings(self):
        conv=(NATIVE/"prepare_model.py").read_text(encoding="utf-8")
        rewrite=(NATIVE/"rewrite_einsum.py").read_text(encoding="utf-8")
        norm=(NATIVE/"normalize_reshapes.py").read_text(encoding="utf-8")
        self.assertIn("check_n=0",conv)
        self.assertIn('"txt_feats":[1,8,512]',conv)
        self.assertIn("GPU_ONLY_RESULT_DIFFERS_FROM_VALIDATED_SHA",conv)
        self.assertIn("UNEXPECTED_RESHAPE_CONVERSION_COUNT",norm)
        self.assertIn('if fixed != 11:',norm)
        self.assertIn('assert converted == 7',rewrite)
        self.assertIn('"MatMul"',rewrite)
        self.assertNotIn("Palworld",conv+rewrite+norm)

    def test_private_sealer_refuses_invalid_path_or_extra_file(self):
        file=NATIVE/"seal_runtime.py"
        spec=importlib.util.spec_from_file_location("asc_v22_native_sealer",file)
        mod=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with tempfile.TemporaryDirectory() as temp:
            parent=Path(temp)
            wrong=parent/"arbitrary-folder"
            wrong.mkdir()
            with self.assertRaisesRegex(RuntimeError,"UNEXPECTED_RUNTIME_PATH"):
                mod.seal(wrong)
            valid=parent/"windows-universal-vision-v22-runtime"
            valid.mkdir()
            (valid/"unknown-secret.txt").write_text("synthetic test",encoding="utf8")
            with self.assertRaisesRegex(RuntimeError,"UNEXPECTED_RUNTIME_FILES"):
                mod.seal(valid)

    def test_source_manifest_prohibited_from_model_distribution(self):
        doc=(ROOT/"docs"/"GAME_AGENT_V22.md").read_text(encoding="utf-8")
        self.assertIn("NOT VERIFIED",doc)
        self.assertIn("No third-party weights",doc)
        self.assertIn("16:9",doc)
        self.assertIn("SHA-256",doc)


if __name__=="__main__":
    unittest.main()
