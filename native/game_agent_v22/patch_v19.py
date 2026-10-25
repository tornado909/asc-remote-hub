"""Generate an isolated v22 WGC native canary from the existing v19 source.

Run only on a separately copied v19 extension. Never patch the installed
signed/sealed v19 extension; v19 remains intact for rollback.
"""
from __future__ import annotations
import hashlib
from pathlib import Path
import sys

if len(sys.argv)!=2:
    raise SystemExit("usage: patch_v19.py <private-copied-v22-native-directory>")
root=Path(sys.argv[1]).resolve()
if root.name!="windows-universal-vision-v22-canary":
    raise RuntimeError("REFUSE_TO_PATCH_ANYTHING_EXCEPT_PRIVATE_V22_STAGING")
source=root/"src"/"consented_wgc_vision.cpp"
if not (root/"include"/"gpu_open_vocab_post.hpp").is_file() or \
   not (root/"include"/"gpu_open_vocab_session.hpp").is_file():
    raise RuntimeError("V22_NATIVE_HEADERS_MISSING")
data=source.read_text(encoding="utf-8")
before=hashlib.sha256(data.encode()).hexdigest()
if before != "975444fe9509304b7ad7b14cc3b59a72bd7f0f1b97546fd1d3d44d5852347b07":
    raise RuntimeError("REFUSE_MODIFIED_OR_UNKNOWN_V19_NATIVE_SOURCE")
def replace(old,new,count=1):
    global data
    found=data.count(old)
    if found!=count:
        raise RuntimeError(f"PATCH_ANCHOR_EXPECTED_{count}_GOT_{found}: {old[:80]!r}")
    data=data.replace(old,new,count)
replace('#include "gpu_vision_session.hpp"',
        '#include "gpu_open_vocab_session.hpp"')
replace('std::filesystem::path model,report;',
        'std::filesystem::path model,report,embeddings;')
replace('else if(std::wcscmp(argv[i],L"--model")==0)a.model=next();',
        'else if(std::wcscmp(argv[i],L"--model")==0)a.model=next();\n'
        '        else if(std::wcscmp(argv[i],L"--embeddings")==0)a.embeddings=next();')
replace('    if(!a.report.empty()&&a.report.extension()!=L".json")',
        '''    if(a.embeddings.empty() ||a.embeddings.extension()!=L".bin" ||
       !std::filesystem::is_regular_file(a.embeddings) ||
       std::filesystem::file_size(a.embeddings)!=ASC_V22_EMBED_BYTES)
       throw std::runtime_error("OPEN_VOCAB_EMBEDDINGS_REQUIRED");
    if(!a.report.empty()&&a.report.extension()!=L".json")''')
replace('    float person=0,bus=0;\n',
        '    std::array<UniversalCompactPost::Detection,ASC_V22_CLASSES> detections{};\n')
replace('    out<<std::fixed<<std::setprecision(4)\n',
        '    out<<std::fixed<<std::setprecision(5)\n')
replace('       <<"\\"version\\":\\"19-canary\\","',
        '       <<"\\"version\\":\\"22-open-vocabulary-canary\\","')
replace('       <<"\\"mode\\":\\"consented_selected_window_WGC_DirectML\\","',
        '       <<"\\"mode\\":\\"consented_selected_window_WGC_DirectML_OpenVocab\\","')
replace('       <<"\\"metadata_to_cpu_bytes\\":"<<(64*records.size())<<","',
        '       <<"\\"metadata_to_cpu_bytes\\":"<<(ASC_V22_METADATA_BYTES*records.size())<<","')
replace('       <<"\\"model_sessions_created\\":1,"',
        '''       <<"\\"model_sessions_created\\":1,"
       <<"\\"semantic_provider\\":\\"YOLO-World-v2\\","
       <<"\\"open_vocabulary\\":true,"
       <<"\\"runtime_text_embedding_classes\\":"<<ASC_V22_CLASSES<<","
       <<"\\"text_embedding_cpu_upload_bytes\\":"<<ASC_V22_EMBED_BYTES<<","''')
replace('           <<",\\"person_confidence\\":"<<rec.person\n'
        '           <<",\\"bus_confidence\\":"<<rec.bus\n',
        '')
replace('        if(rec.diagnostic)\n',
        '''        out<<",\\"detections\\":[";
        for(UINT cls=0;cls<ASC_V22_CLASSES;++cls){
           if(cls)out<<",";
           const auto& det=rec.detections[cls];
           out<<"{\\"prompt_index\\":"<<cls<<",\\"score\\":"<<det.confidence
              <<",\\"anchor\\":"<<det.anchor<<",\\"box_model_normalized\\":["
              <<det.normalizedBox[0]<<","<<det.normalizedBox[1]<<","
              <<det.normalizedBox[2]<<","<<det.normalizedBox[3]<<"]}";
        }
        out<<"]";
        if(rec.diagnostic)
''')
replace('        GpuVision vision(bridge,args.model);',
        '        GpuVision vision(bridge,args.model,args.embeddings);')
replace('            current.person=prediction.compact.person;\n'
        '            current.bus=prediction.compact.bus;\n',
        '            current.detections=prediction.grounded.detections;\n')
if len(data)<17000 or "person_confidence" in data:
    raise RuntimeError("UNEXPECTED_V22_TRANSFORM")
source.write_text(data,encoding="utf-8")
print("NATIVE_V22_SOURCE_PATCHED",{
    "original_sha256":before,
    "new_sha256":hashlib.sha256(data.encode()).hexdigest(),
    "bytes":len(data),
    "v19_installation_touched":False
})
