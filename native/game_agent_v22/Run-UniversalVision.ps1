# ASC Game Agent v22: private, sealed, Windows-only research canary.
# The public Remote Hub does not redistribute the third-party model or DLLs.
# Capture of an external window ALWAYS invokes the native local Yes/No dialog.
[CmdletBinding()]
param(
    [ValidateSet('Doctor','Fixture','Preflight','Capture')]
    [string]$Action='Doctor',
    [string]$Hwnd='',
    [long]$ProcessId=0,
    [long]$ProcessStartTicks=0,
    [ValidateRange(2,12)][int]$Frames=4
)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'

function Digest([string]$Path) {
    $hash=[Security.Cryptography.SHA256]::Create()
    $stream=[IO.File]::OpenRead($Path)
    try {
        return ([BitConverter]::ToString($hash.ComputeHash($stream))).Replace('-','').ToLowerInvariant()
    } finally {
        $stream.Dispose()
        $hash.Dispose()
    }
}
$root=[IO.Path]::GetFullPath($PSScriptRoot)
$manifestPath=Join-Path $root 'SHA256SUMS.json'
$manifest=Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
if($manifest.schema -ne 2 -or
   $manifest.release -cne 'asc-game-agent-universal-vision-v22-private-canary' -or
   $manifest.third_party_weights_bundled_publicly -ne $false -or
   $manifest.local_native_consent_required -ne $true) {
    throw 'ASC_V22_MANIFEST_UNTRUSTED'
}
$names=@{}
foreach($entry in $manifest.files) {
    $name=[string]$entry.path
    if([string]::IsNullOrEmpty($name) -or
       $name -cnotmatch '^[A-Za-z0-9_\-\.]+(?:/[A-Za-z0-9_\-\.]+)*$' -or
       $name -match '(^|/)\.\.?(/|$)' -or
       $names.ContainsKey($name)) {
       throw 'ASC_V22_FILE_NAME_UNSAFE'
    }
    $names[$name]=$true
    $target=Join-Path $root ($name.Replace('/','\'))
    $item=Get-Item -LiteralPath $target -ErrorAction Stop
    if($item.PSIsContainer -or
       ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
       $item.Length -ne [int64]$entry.bytes -or
       (Digest $target) -cne ([string]$entry.sha256).ToLowerInvariant()) {
        throw 'ASC_V22_LOCAL_FILE_INTEGRITY_INVALID'
    }
}
$expected=@(
    'bin/asc_consent_wgc_vision.exe',
    'bin/onnxruntime.dll',
    'bin/DirectML.dll',
    'bin/onnxruntime_providers_shared.dll',
    'models/yolov8s-worldv2-static8-reshape-normalized.onnx',
    'models/yoloworld_clip8_features.bin',
    'models/yoloworld_clip8_features.json',
    'Run-UniversalVision.ps1'
)
if($names.Count -ne $expected.Count) {throw 'ASC_V22_MANIFEST_INCOMPLETE'}
foreach($item in $expected) {
    if(-not $names.ContainsKey($item)) {throw 'ASC_V22_MISSING_REQUIRED_FILE'}
}
$seen=@{}
foreach($directory in (Get-ChildItem -LiteralPath $root -Recurse -Directory -Force)) {
    if($directory.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw 'ASC_V22_UNEXPECTED_REPARSE_DIRECTORY'
    }
}
foreach($f in (Get-ChildItem -LiteralPath $root -Recurse -File -Force)) {
    if($f.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw 'ASC_V22_UNEXPECTED_SYMLINK'
    }
    $rel=$f.FullName.Substring($root.Length+1).Replace('\','/')
    if($rel -cne 'SHA256SUMS.json' -and -not $names.ContainsKey($rel)) {
        throw 'ASC_V22_UNSEALED_RUNTIME_FILE'
    }
    $seen[$rel]=$true
}
if($seen.Count -ne ($names.Count+1)) {throw 'ASC_V22_UNEXPECTED_RUNTIME_CONTENTS'}

$meta=Get-Content -LiteralPath (Join-Path $root 'models\yoloworld_clip8_features.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if($meta.model -cne 'openai/clip-vit-base-patch32' -or
   $meta.tensor_shape.Count -ne 3 -or
   $meta.tensor_shape[0] -ne 1 -or $meta.tensor_shape[1] -ne 8 -or
   $meta.tensor_shape[2] -ne 512 -or
   $meta.prompts.Count -ne 8 -or
   $meta.dtype -cne 'float32' -or
   ([string]$meta.sha256).ToLowerInvariant() -cne (Digest (Join-Path $root 'models\yoloworld_clip8_features.bin'))) {
    throw 'ASC_V22_PROMPTS_HASH_OR_SHAPE_MISMATCH'
}
$exe=Join-Path $root 'bin\asc_consent_wgc_vision.exe'
$model=Join-Path $root 'models\yolov8s-worldv2-static8-reshape-normalized.onnx'
$embeddings=Join-Path $root 'models\yoloworld_clip8_features.bin'

if($Action -eq 'Doctor') {
    [pscustomobject]@{
        ok=$true
        code='ASC_V22_PRIVATE_CANARY_VERIFIED'
        version='22-private'
        prompt_labels=@($meta.prompts)
        bundled_weights_publicly=$false
        gpu_initialized=$false
        captured=$false
        game_started=$false
        input_sent=$false
        local_consent_required_for_external_window=$true
    } | ConvertTo-Json -Compress -Depth 5
    return
}
# A locally approved opt-in is mandatory for GPU usage. No remote MCP
# invocation can turn it on. This helper is NOT an ACL for users already
# authorized to execute arbitrary code under this Windows account.
$cfg=Join-Path $env:APPDATA 'ASC-Remote-Hub\agent.json'
if(-not (Test-Path -LiteralPath $cfg)) {throw 'ASC_V22_ENROLLED_LOCAL_CONFIG_REQUIRED'}
$policy=Get-Content -LiteralPath $cfg -Raw -Encoding UTF8 | ConvertFrom-Json
if($policy.permissions.exec -ne $true -or $policy.permissions.gui -ne $true -or
   $policy.game_agent.enabled -ne $true) {
    throw 'ASC_V22_LOCAL_PERMISSION_DISABLED'
}
if($Action -eq 'Fixture' -and $policy.game_agent.gpu_test -ne $true) {
    throw 'ASC_V22_GPU_TEST_DISABLED'
}
if($Action -eq 'Capture' -and $policy.game_agent.capture -ne $true) {
    throw 'ASC_V22_CAPTURE_DISABLED'
}
if($Action -in @('Capture','Preflight')) {
    if($Hwnd -cnotmatch '^(0[xX][0-9a-fA-F]{1,16}|[0-9]{1,20})$' -or
       $ProcessId -le 0 -or $ProcessStartTicks -le 0) {
        throw 'ASC_V22_TARGET_HWND_PID_START_REQUIRED'
    }
}
$args=@('--model',$model,'--embeddings',$embeddings)
if($Action -eq 'Fixture') {
    $args+=@('--fixture-test','--fixture-width','960','--fixture-height','540',
             '--frames',[string]$Frames)
} elseif($Action -eq 'Capture') {
    $args+=@('--target-hwnd',$Hwnd,'--expect-pid',[string]$ProcessId,
             '--expect-start',[string]$ProcessStartTicks,'--frames',[string]$Frames)
} else {
    $args+=@('--target-hwnd',$Hwnd,'--expect-pid',[string]$ProcessId,
             '--expect-start',[string]$ProcessStartTicks,'--check-only')
}
$report=$null
if($Action -ne 'Preflight') {
    $results=Join-Path $env:LOCALAPPDATA 'ASC-Game-Agent\results'
    New-Item -ItemType Directory -Path $results -Force | Out-Null
    $report=Join-Path $results ('universal-v22-'+[guid]::NewGuid().ToString('N')+'.json')
    $args+=@('--report',$report)
}
$quoteArgs=@()
foreach($arg in $args) {
    if([string]$arg -match '[\"\r\n]') {throw 'ASC_V22_ARG_QUOTE_INVALID'}
    $quoteArgs+=('"'+[string]$arg+'"')
}
$psi=New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName=$exe
$psi.Arguments=($quoteArgs -join ' ')
$psi.UseShellExecute=$false
$psi.CreateNoWindow=$true
$psi.RedirectStandardOutput=$true
$psi.RedirectStandardError=$true
$psi.StandardOutputEncoding=[System.Text.UTF8Encoding]::new($false)
$psi.StandardErrorEncoding=[System.Text.UTF8Encoding]::new($false)
$proc=New-Object System.Diagnostics.Process
$proc.StartInfo=$psi
try {
    if(-not $proc.Start()) {throw 'ASC_V22_WORKER_NOT_STARTED'}
    $stdoutTask=$proc.StandardOutput.ReadToEndAsync()
    $stderrTask=$proc.StandardError.ReadToEndAsync()
    if(-not $proc.WaitForExit(35000)) {
        $proc.Kill()
        [void]$proc.WaitForExit(3000)
        throw 'ASC_V22_CONSENT_OR_CAPTURE_TIMEOUT'
    }
    $stdout=$stdoutTask.GetAwaiter().GetResult()
    $stderr=$stderrTask.GetAwaiter().GetResult()
    if($proc.ExitCode -ne 0) {
        throw ('ASC_V22_NATIVE_REJECTED:'+ $proc.ExitCode)
    }
    if($Action -eq 'Preflight') {
        $info=$stdout|ConvertFrom-Json
        if($info.capture_authorized -eq $true) {throw 'ASC_V22_PREFLIGHT_GRANTED_CAPTURE'}
        $info|ConvertTo-Json -Compress -Depth 5
    } else {
        $info=Get-Content -LiteralPath $report -Raw -Encoding UTF8 | ConvertFrom-Json
        if($info.ok -ne $true -or $info.version -cne '22-open-vocabulary-canary' -or
           $info.gpu_only -ne $true -or $info.cpu_fallback_disabled -ne $true -or
           $info.full_frames_to_cpu_bytes -ne 0 -or
           $info.host_input_sent -ne $false -or
           ($Action -eq 'Capture' -and $info.consent_origin -cne 'native_local_dialog')) {
           throw 'ASC_V22_NATIVE_SAFETY_INVARIANTS_FAILED'
        }
        $info|ConvertTo-Json -Compress -Depth 12
    }
} finally {
    $proc.Dispose()
    # Only delete this helper's freshly generated metadata report. Never
    # retain window details, and never remove unrelated files.
    if($report -and (Test-Path -LiteralPath $report -PathType Leaf)) {
        Remove-Item -LiteralPath $report -Force -ErrorAction SilentlyContinue
    }
}
