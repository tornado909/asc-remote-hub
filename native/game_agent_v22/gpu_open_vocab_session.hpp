#pragma once
// ASC Game Agent v22: WGC GPU texture -> D3D12 -> DirectML YOLO-World.
// Two GPU inputs: captured image tensor, 8 runtime CLIP text embeddings.
// No GPU pixel/tensor/logit CPU fallback; return only 256B GPU-reduced metadata.
#include "d3d11on12_bridge.hpp"
#include "gpu_open_vocab_post.hpp"
#include "onnxruntime_session_options_config_keys.h"
#include <filesystem>
#include <fstream>
#include <cmath>
#include <memory>
#include <cstring>

namespace {
double milliseconds(Clock::duration delta) {
    return std::chrono::duration<double,std::milli>(delta).count();
}
struct GpuVision {
    Bridge& bridge;
    ComPtr<IDMLDevice> dml;
    Ort::Env env{ORT_LOGGING_LEVEL_WARNING,"ASC-WGC-OpenVocabulary-v22"};
    Ort::SessionOptions options;
    std::unique_ptr<Ort::Session> session;
    const OrtDmlApi* api=nullptr;
    ComPtr<ID3D12Resource> tensor,features,logits;
    std::unique_ptr<UniversalCompactPost> post;
    std::string inName,featName,outName;
    bool tensorCommonAfterFirstRun=false;

    GpuVision(Bridge& b,const std::filesystem::path& model,
              const std::filesystem::path& embeddings):bridge(b) {
        if(model.extension()!=L".onnx"||!std::filesystem::is_regular_file(model))
            throw std::runtime_error("OPEN_VOCAB_ONNX_MODEL_NOT_FOUND");
        if(embeddings.extension()!=L".bin"||
           !std::filesystem::is_regular_file(embeddings)||
           std::filesystem::file_size(embeddings)!=ASC_V22_EMBED_BYTES)
            throw std::runtime_error("OPEN_VOCAB_CLIP_EMBEDDINGS_INVALID");
        // Conservative headroom to avoid starving the foreground game.
        ComPtr<IDXGIAdapter3> a3;
        if(SUCCEEDED(bridge.adapter.As(&a3))){
           DXGI_QUERY_VIDEO_MEMORY_INFO info{};
           if(SUCCEEDED(a3->QueryVideoMemoryInfo(0,DXGI_MEMORY_SEGMENT_GROUP_LOCAL,&info))) {
             const UINT64 freeBudget=info.Budget>info.CurrentUsage?
                       info.Budget-info.CurrentUsage:0;
             if(freeBudget<1024ull*1024ull*1024ull)
                  throw std::runtime_error("OPEN_VOCAB_GPU_BUDGET_LT_1G");
           }
        }

        hr_ok(DMLCreateDevice(b.device.Get(),DML_CREATE_DEVICE_FLAG_NONE,
                              IID_PPV_ARGS(&dml)),"UniversalDMLCreateDevice");
        options.DisableMemPattern();
        options.SetExecutionMode(ExecutionMode::ORT_SEQUENTIAL);
        options.AddConfigEntry(kOrtSessionOptionsDisableCPUEPFallback,"1");
        Ort::ThrowOnError(Ort::GetApi().GetExecutionProviderApi(
               "DML",ORT_API_VERSION,reinterpret_cast<const void**>(&api)));
        if(!api)throw std::runtime_error("OPEN_VOCAB_DML_PROVIDER_UNAVAILABLE");
        Ort::ThrowOnError(api->SessionOptionsAppendExecutionProvider_DML1(
               options,dml.Get(),b.queue.Get()));
        session=std::make_unique<Ort::Session>(env,model.c_str(),options);
        if(session->GetInputCount()!=2 ||session->GetOutputCount()!=1)
           throw std::runtime_error("OPEN_VOCAB_MODEL_INPUT_CONTRACT");
        const auto shapeImg=session->GetInputTypeInfo(0).GetTensorTypeAndShapeInfo().GetShape();
        const auto shapeTxt=session->GetInputTypeInfo(1).GetTensorTypeAndShapeInfo().GetShape();
        const auto shapeOut=session->GetOutputTypeInfo(0).GetTensorTypeAndShapeInfo().GetShape();
        if(shapeImg!=std::vector<int64_t>{1,3,480,640}||
           shapeTxt!=std::vector<int64_t>{1,8,512}||
           shapeOut!=std::vector<int64_t>{1,12,6300})
            throw std::runtime_error("OPEN_VOCAB_ONNX_STATIC_SHAPES_INVALID");
        Ort::AllocatorWithDefaultOptions alloc;
        inName=session->GetInputNameAllocated(0,alloc).get();
        featName=session->GetInputNameAllocated(1,alloc).get();
        outName=session->GetOutputNameAllocated(0,alloc).get();
        if(inName!="images"||featName!="txt_feats")
            throw std::runtime_error("OPEN_VOCAB_ONNX_INPUT_NAMES_INVALID");

        tensor=buffer_resource(b.device.Get(),TENSOR_BYTES,D3D12_HEAP_TYPE_DEFAULT,
                    D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS,
                    D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
        features=buffer_resource(b.device.Get(),ASC_V22_EMBED_BYTES,
                    D3D12_HEAP_TYPE_DEFAULT,
                    D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS,
                    D3D12_RESOURCE_STATE_COPY_DEST);
        logits=buffer_resource(b.device.Get(),ASC_V22_LOGITS_BYTES,D3D12_HEAP_TYPE_DEFAULT,
                    D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS,
                    D3D12_RESOURCE_STATE_COMMON);
        // Text-only small embeddings are permitted to be uploaded from CPU
        // exactly ONCE. Captured image data NEVER travels through this upload.
        auto upload=buffer_resource(b.device.Get(),ASC_V22_EMBED_BYTES,
                    D3D12_HEAP_TYPE_UPLOAD,D3D12_RESOURCE_FLAG_NONE,
                    D3D12_RESOURCE_STATE_GENERIC_READ);
        void* mapped=nullptr;
        hr_ok(upload->Map(0,nullptr,&mapped),"MapClipFeatureUpload");
        std::ifstream file(embeddings,std::ios::binary);
        if(!file.read(static_cast<char*>(mapped),ASC_V22_EMBED_BYTES)) {
            upload->Unmap(0,nullptr);
            throw std::runtime_error("OPEN_VOCAB_EMBEDDINGS_READ_FAILED");
        }
        upload->Unmap(0,nullptr);
        hr_ok(b.alloc->Reset(),"UploadClipAllocator");
        hr_ok(b.list->Reset(b.alloc.Get(),nullptr),"UploadClipCommandList");
        b.list->CopyBufferRegion(features.Get(),0,upload.Get(),0,ASC_V22_EMBED_BYTES);
        D3D12_RESOURCE_BARRIER featureBarrier{};
        featureBarrier.Type=D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
        featureBarrier.Transition.pResource=features.Get();
        featureBarrier.Transition.Subresource=D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
        featureBarrier.Transition.StateBefore=D3D12_RESOURCE_STATE_COPY_DEST;
        featureBarrier.Transition.StateAfter=D3D12_RESOURCE_STATE_COMMON;
        b.list->ResourceBarrier(1,&featureBarrier);
        hr_ok(b.list->Close(),"UploadClipClose");
        ID3D12CommandList* cmds[]={b.list.Get()};
        b.queue->ExecuteCommandLists(1,cmds);
        wait_gpu(b.queue.Get(),b.fence.Get(),++b.seq);
        upload.Reset();
        post=std::make_unique<UniversalCompactPost>(b.device.Get(),logits.Get());
    }

    struct Result {
        UniversalCompactPost::Result grounded{};
        double preprocessing_ms=0;
        double inference_submit_ms=0;
        double gpu_reduce_and_fence_ms=0;
    };

    Result run_on_last_wgc_texture(){
        if(bridge.current!=D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE)
            throw std::runtime_error("OPEN_VOCAB_BRIDGE_TEXTURE_UNEXPECTED_STATE");
        const auto started=Clock::now();
        hr_ok(bridge.alloc->Reset(),"UniversalPreprocessAllocator");
        hr_ok(bridge.list->Reset(bridge.alloc.Get(),nullptr),"UniversalPreprocessCommand");
        if(tensorCommonAfterFirstRun){
            D3D12_RESOURCE_BARRIER barrier{};
            barrier.Type=D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
            barrier.Transition.pResource=tensor.Get();
            barrier.Transition.Subresource=D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
            barrier.Transition.StateBefore=D3D12_RESOURCE_STATE_COMMON;
            barrier.Transition.StateAfter=D3D12_RESOURCE_STATE_UNORDERED_ACCESS;
            bridge.list->ResourceBarrier(1,&barrier);
        }
        gpu_convert(bridge.device.Get(),bridge.list.Get(),
                    bridge.gpuTexture.Get(),tensor.Get());
        tensorCommonAfterFirstRun=true;
        hr_ok(bridge.list->Close(),"UniversalPreprocessClose");
        ID3D12CommandList* cmds[]={bridge.list.Get()};
        bridge.queue->ExecuteCommandLists(1,cmds);
        wait_gpu(bridge.queue.Get(),bridge.fence.Get(),++bridge.seq);
        const auto preDone=Clock::now();

        void *allocationImg=nullptr,*allocationFeatures=nullptr,*allocationOut=nullptr;
        auto releaseAll=[&](){
           if(allocationOut){api->FreeGPUAllocation(allocationOut);allocationOut=nullptr;}
           if(allocationFeatures){api->FreeGPUAllocation(allocationFeatures);allocationFeatures=nullptr;}
           if(allocationImg){api->FreeGPUAllocation(allocationImg);allocationImg=nullptr;}
        };
        try{
           Ort::ThrowOnError(api->CreateGPUAllocationFromD3DResource(tensor.Get(),&allocationImg));
           Ort::ThrowOnError(api->CreateGPUAllocationFromD3DResource(features.Get(),&allocationFeatures));
           Ort::ThrowOnError(api->CreateGPUAllocationFromD3DResource(logits.Get(),&allocationOut));
           if(!allocationImg||!allocationFeatures||!allocationOut)
               throw std::runtime_error("OPEN_VOCAB_GPU_ALLOCATION_FAILED");
           Ort::MemoryInfo memory("DML",OrtDeviceAllocator,0,OrtMemTypeDefault);
           std::array<int64_t,4> inputShape{1,3,480,640};
           std::array<int64_t,3> promptShape{1,8,512};
           std::array<int64_t,3> outputShape{1,12,6300};
           auto input=Ort::Value::CreateTensor(memory,allocationImg,TENSOR_BYTES,
                       inputShape.data(),inputShape.size(),ONNX_TENSOR_ELEMENT_DATA_TYPE_FLOAT);
           auto embedding=Ort::Value::CreateTensor(memory,allocationFeatures,
                       ASC_V22_EMBED_BYTES,promptShape.data(),promptShape.size(),
                       ONNX_TENSOR_ELEMENT_DATA_TYPE_FLOAT);
           auto output=Ort::Value::CreateTensor(memory,allocationOut,
                       ASC_V22_LOGITS_BYTES,outputShape.data(),outputShape.size(),
                       ONNX_TENSOR_ELEMENT_DATA_TYPE_FLOAT);
           const auto infer=Clock::now();
           {
             Ort::IoBinding binding(*session);
             binding.BindInput(inName.c_str(),input);
             binding.BindInput(featName.c_str(),embedding);
             binding.BindOutput(outName.c_str(),output);
             session->Run(Ort::RunOptions{nullptr},binding);
           }
           const auto submitted=Clock::now();
           auto metadata=post->Run(bridge.queue.Get());
           const auto done=Clock::now();
           input=Ort::Value{nullptr};
           embedding=Ort::Value{nullptr};
           output=Ort::Value{nullptr};
           releaseAll();
           return {metadata,milliseconds(preDone-started),
                   milliseconds(submitted-infer),milliseconds(done-submitted)};
        }catch(...){
           releaseAll();
           throw;
        }
    }
};
}
