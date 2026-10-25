#pragma once
// ASC Game Agent v22 experimental universal open-vocabulary GPU reduction.
// Processes NO pixels on CPU; only eight 32-byte metadata records per frame.
// Labels are provided separately by text embeddings at runtime.
#include "asc_d3d12_common.hpp"
#include <cmath>
#include <cstring>

static constexpr UINT ASC_V22_CLASSES=8;
static constexpr UINT ASC_V22_ANCHORS=6300;
static constexpr UINT64 ASC_V22_EMBED_BYTES=UINT64(ASC_V22_CLASSES)*512ull*4ull;
static constexpr UINT64 ASC_V22_LOGITS_BYTES=UINT64(4+ASC_V22_CLASSES)*ASC_V22_ANCHORS*4ull;
static constexpr UINT64 ASC_V22_METADATA_BYTES=UINT64(ASC_V22_CLASSES)*32ull;

class UniversalCompactPost {
  ComPtr<ID3D12Device> device_;
  ComPtr<ID3D12Resource> logits_,meta_,readback_;
  ComPtr<ID3D12RootSignature> root_;
  ComPtr<ID3D12PipelineState> pipeline_;
  ComPtr<ID3D12DescriptorHeap> heap_;
  ComPtr<ID3D12CommandAllocator> allocator_;
  ComPtr<ID3D12GraphicsCommandList> commands_;
  ComPtr<ID3D12Fence> fence_;
  uint64_t sequence_=0;
  D3D12_RESOURCE_STATES metaState_=D3D12_RESOURCE_STATE_UNORDERED_ACCESS;
public:
  struct Detection {
    float confidence=0;
    uint32_t anchor=0;
    float normalizedBox[4]{};
  };
  struct Result {
    std::array<Detection,ASC_V22_CLASSES> detections{};
  };
  UniversalCompactPost(ID3D12Device* dev,ID3D12Resource* logits):
      device_(dev),logits_(logits) {
    const auto desc=logits_->GetDesc();
    if(desc.Dimension!=D3D12_RESOURCE_DIMENSION_BUFFER ||
       desc.Width!=ASC_V22_LOGITS_BYTES ||
       !(desc.Flags&D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS))
       throw std::runtime_error("OPEN_VOCAB_LOGITS_RESOURCE_INVALID");
    meta_=buffer_resource(dev,ASC_V22_METADATA_BYTES,D3D12_HEAP_TYPE_DEFAULT,
                          D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS,
                          D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
    readback_=buffer_resource(dev,ASC_V22_METADATA_BYTES,D3D12_HEAP_TYPE_READBACK,
                              D3D12_RESOURCE_FLAG_NONE,D3D12_RESOURCE_STATE_COPY_DEST);
    const char shader[]=R"ASC_V22_HLSL(
       ByteAddressBuffer Logits:register(t0);
       RWByteAddressBuffer Output:register(u0);
       groupshared float Best[256];
       groupshared uint Anchor[256];
       [numthreads(256,1,1)]
       void CSMain(uint3 group:SV_GroupID, uint3 laneId:SV_GroupThreadID) {
          uint cls=group.x;
          uint lane=laneId.x;
          float peak=-3.402823e38;
          uint selected=0;
          for(uint i=lane;i<6300;i+=256) {
             float score=asfloat(Logits.Load(((4+cls)*6300+i)*4));
             if(isfinite(score) && score>peak) {
                peak=score;
                selected=i;
             }
          }
          Best[lane]=peak;
          Anchor[lane]=selected;
          GroupMemoryBarrierWithGroupSync();
          for(uint stride=128;stride>0;stride>>=1) {
             if(lane<stride && Best[lane+stride]>Best[lane]) {
                Best[lane]=Best[lane+stride];
                Anchor[lane]=Anchor[lane+stride];
             }
             GroupMemoryBarrierWithGroupSync();
          }
          if(lane==0) {
             uint idx=Anchor[0];
             uint offset=cls*32;
             float x=asfloat(Logits.Load((idx)*4));
             float y=asfloat(Logits.Load((6300+idx)*4));
             float w=asfloat(Logits.Load((12600+idx)*4));
             float h=asfloat(Logits.Load((18900+idx)*4));
             float sx=saturate((x-w*0.5f)/640.0f);
             float sy=saturate((y-h*0.5f)/480.0f);
             float ex=saturate((x+w*0.5f)/640.0f);
             float ey=saturate((y+h*0.5f)/480.0f);
             Output.Store(offset,asuint(saturate(Best[0])));
             Output.Store(offset+4,idx);
             Output.Store(offset+8,asuint(sx));
             Output.Store(offset+12,asuint(sy));
             Output.Store(offset+16,asuint(ex));
             Output.Store(offset+20,asuint(ey));
             Output.Store(offset+24,0);
             Output.Store(offset+28,0);
          }
       }
    )ASC_V22_HLSL";
    ComPtr<ID3DBlob> cs,error;
    const auto compile=D3DCompile(shader,sizeof(shader)-1,nullptr,nullptr,nullptr,
                  "CSMain","cs_5_1",D3DCOMPILE_OPTIMIZATION_LEVEL3,0,&cs,&error);
    if(FAILED(compile) && error)
        std::cerr<<"ASC_V22_HLSL "<<std::string(
                     static_cast<const char*>(error->GetBufferPointer()),
                     error->GetBufferSize())<<"\n";
    hr_ok(compile,"CompileUniversalGpuPostprocess");
    D3D12_DESCRIPTOR_RANGE ranges[2]{};
    D3D12_ROOT_PARAMETER parameters[2]{};
    for(int i=0;i<2;++i){
        ranges[i].RangeType=i?D3D12_DESCRIPTOR_RANGE_TYPE_UAV:D3D12_DESCRIPTOR_RANGE_TYPE_SRV;
        ranges[i].NumDescriptors=1;
        ranges[i].BaseShaderRegister=0;
        ranges[i].OffsetInDescriptorsFromTableStart=D3D12_DESCRIPTOR_RANGE_OFFSET_APPEND;
        parameters[i].ParameterType=D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
        parameters[i].DescriptorTable.NumDescriptorRanges=1;
        parameters[i].DescriptorTable.pDescriptorRanges=&ranges[i];
        parameters[i].ShaderVisibility=D3D12_SHADER_VISIBILITY_ALL;
    }
    D3D12_ROOT_SIGNATURE_DESC descSig{};
    descSig.NumParameters=2;descSig.pParameters=parameters;
    ComPtr<ID3DBlob> serialized,serializationError;
    auto hr=D3D12SerializeRootSignature(&descSig,D3D_ROOT_SIGNATURE_VERSION_1,
                                       &serialized,&serializationError);
    hr_ok(hr,"UniversalPostRootSerialize");
    hr_ok(dev->CreateRootSignature(0,serialized->GetBufferPointer(),
                                  serialized->GetBufferSize(),IID_PPV_ARGS(&root_)),
          "UniversalPostRoot");
    D3D12_COMPUTE_PIPELINE_STATE_DESC state{};
    state.pRootSignature=root_.Get();
    state.CS={cs->GetBufferPointer(),cs->GetBufferSize()};
    hr_ok(dev->CreateComputePipelineState(&state,IID_PPV_ARGS(&pipeline_)),
          "UniversalPostPipeline");
    D3D12_DESCRIPTOR_HEAP_DESC heapdesc{};
    heapdesc.Type=D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV;
    heapdesc.NumDescriptors=2;
    heapdesc.Flags=D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE;
    hr_ok(dev->CreateDescriptorHeap(&heapdesc,IID_PPV_ARGS(&heap_)),"UniversalPostHeap");
    auto handle=heap_->GetCPUDescriptorHandleForHeapStart();
    D3D12_SHADER_RESOURCE_VIEW_DESC srv{};
    srv.Format=DXGI_FORMAT_R32_TYPELESS;
    srv.ViewDimension=D3D12_SRV_DIMENSION_BUFFER;
    srv.Shader4ComponentMapping=D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING;
    srv.Buffer.NumElements=UINT(ASC_V22_LOGITS_BYTES/4);
    srv.Buffer.Flags=D3D12_BUFFER_SRV_FLAG_RAW;
    dev->CreateShaderResourceView(logits_.Get(),&srv,handle);
    handle.ptr+=dev->GetDescriptorHandleIncrementSize(heapdesc.Type);
    D3D12_UNORDERED_ACCESS_VIEW_DESC uav{};
    uav.Format=DXGI_FORMAT_R32_TYPELESS;
    uav.ViewDimension=D3D12_UAV_DIMENSION_BUFFER;
    uav.Buffer.NumElements=UINT(ASC_V22_METADATA_BYTES/4);
    uav.Buffer.Flags=D3D12_BUFFER_UAV_FLAG_RAW;
    dev->CreateUnorderedAccessView(meta_.Get(),nullptr,&uav,handle);
    hr_ok(dev->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT,
                                      IID_PPV_ARGS(&allocator_)),"UniversalPostAllocator");
    hr_ok(dev->CreateCommandList(0,D3D12_COMMAND_LIST_TYPE_DIRECT,
                                 allocator_.Get(),nullptr,
                                 IID_PPV_ARGS(&commands_)),"UniversalPostCommandList");
    hr_ok(commands_->Close(),"UniversalPostInitialClose");
    hr_ok(dev->CreateFence(0,D3D12_FENCE_FLAG_NONE,IID_PPV_ARGS(&fence_)),
          "UniversalPostFence");
  }

  Result Run(ID3D12CommandQueue* queue){
    hr_ok(allocator_->Reset(),"UniversalPostAllocatorReset");
    hr_ok(commands_->Reset(allocator_.Get(),nullptr),"UniversalPostReset");
    if(metaState_!=D3D12_RESOURCE_STATE_UNORDERED_ACCESS){
      D3D12_RESOURCE_BARRIER transition{};
      transition.Type=D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
      transition.Transition.pResource=meta_.Get();
      transition.Transition.Subresource=D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
      transition.Transition.StateBefore=metaState_;
      transition.Transition.StateAfter=D3D12_RESOURCE_STATE_UNORDERED_ACCESS;
      commands_->ResourceBarrier(1,&transition);
      metaState_=D3D12_RESOURCE_STATE_UNORDERED_ACCESS;
    }
    ID3D12DescriptorHeap* heaps[]={heap_.Get()};
    commands_->SetDescriptorHeaps(1,heaps);
    commands_->SetPipelineState(pipeline_.Get());
    commands_->SetComputeRootSignature(root_.Get());
    auto gpu=heap_->GetGPUDescriptorHandleForHeapStart();
    commands_->SetComputeRootDescriptorTable(0,gpu);
    gpu.ptr+=device_->GetDescriptorHandleIncrementSize(
           D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);
    commands_->SetComputeRootDescriptorTable(1,gpu);
    commands_->Dispatch(ASC_V22_CLASSES,1,1);
    D3D12_RESOURCE_BARRIER trans{};
    trans.Type=D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    trans.Transition.pResource=meta_.Get();
    trans.Transition.Subresource=D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
    trans.Transition.StateBefore=D3D12_RESOURCE_STATE_UNORDERED_ACCESS;
    trans.Transition.StateAfter=D3D12_RESOURCE_STATE_COPY_SOURCE;
    commands_->ResourceBarrier(1,&trans);
    metaState_=D3D12_RESOURCE_STATE_COPY_SOURCE;
    commands_->CopyBufferRegion(readback_.Get(),0,meta_.Get(),0,ASC_V22_METADATA_BYTES);
    hr_ok(commands_->Close(),"UniversalPostClose");
    ID3D12CommandList* cmd[]={commands_.Get()};
    queue->ExecuteCommandLists(1,cmd);
    wait_queue(queue,fence_.Get(),++sequence_);
    void* ptr=nullptr;
    D3D12_RANGE range{0,ASC_V22_METADATA_BYTES};
    hr_ok(readback_->Map(0,&range,&ptr),"UniversalReadbackMap");
    const uint8_t* bytes=static_cast<const uint8_t*>(ptr);
    Result result{};
    for(UINT cls=0;cls<ASC_V22_CLASSES;++cls){
        const auto* item=bytes+cls*32;
        auto& d=result.detections[cls];
        std::memcpy(&d.confidence,item,4);
        std::memcpy(&d.anchor,item+4,4);
        std::memcpy(d.normalizedBox,item+8,16);
    }
    D3D12_RANGE empty{0,0};
    readback_->Unmap(0,&empty);
    for(const auto& d:result.detections){
       if(!std::isfinite(d.confidence)||d.confidence<0||d.confidence>1||
          d.anchor>=ASC_V22_ANCHORS)
          throw std::runtime_error("UNIVERSAL_GPU_METADATA_INVALID");
       for(float coord:d.normalizedBox)
          if(!std::isfinite(coord)||coord<0||coord>1)
             throw std::runtime_error("UNIVERSAL_GPU_BOX_INVALID");
    }
    return result;
  }
};
