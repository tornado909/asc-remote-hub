# ASC Game Agent: optional Windows GPU bridge (research canary)

**Universal game-independent scene architecture:** [Universal Perception Core v21](GAME_AGENT_UNIVERSAL.md) and [isolated YOLO-World v22 research canary](GAME_AGENT_V22.md). Production v19 remains COCO person/bus diagnostic-only. Neither VLM/OCR nor autonomous game control has been added.

The public ASC Remote Hub **does not include games, DirectML DLLs, ONNX Runtime,
YOLO weights or native ASC Game Agent binaries**. This bridge connects to
independently installed, locally trusted and manifest-verified **v18 Window
Targets** and **v19 Consented Capture** packages. It never installs them.

This is an experimental Windows-only integration. It is **not a gaming autopilot**.

## Requirements and local opt-in

1. Install independently supplied, trusted Windows components
   **windows-targets-v18** and **windows-consented-capture-v19** under
   the current user's %LOCALAPPDATA%\ASC-Game-Agent\extensions directory.
   These native packages are NOT redistributed by the public Hub installer.
2. Pair the Windows Remote Hub agent with execution and GUI permissions.
   Read-only agents cannot enable Game Agent.
3. In the **local Windows tray** (right-click ASC Remote Hub), open
   **ASC Game Agent (canary)** and choose **Включить Game Agent**.
   The tray stops the worker, saves the option atomically to the current
   user's ASC-Remote-Hub agent.json, then restarts the worker.
4. **Разрешить GPU-самотест** enables a requested GPU test on a temporary
   self-owned Win32 window. The GPU does not run continuously.
5. **Разрешить запрос захвата окна** enables requests to show a separate
   native Windows consent prompt. Every capture of another application
   still requires an actual human **Yes** locally; it cannot be approved
   by ChatGPT or another remote MCP client.

All options are **OFF by default**, even if environment variables are
inherited. Existing device enrollment, OAuth credentials and DPAPI secrets
are preserved. Disabling Game Agent also clears the two GPU options.

Full remote command execution is itself a powerful permission. The local
Game Agent option is **not a security sandbox against a client already
authorized to run arbitrary commands as the same Windows user**.

## Five MCP tools

| Tool | Behavior |
| --- | --- |
| **game_agent_status** | Validates v18/v19 sealed package wrappers; no window list or GPU. |
| **game_agent_targets** | Lists eligible Win32 windows with HWND, PID, process creation ticks, application name/title. No capture. |
| **game_agent_preflight** | Rechecks exact target identity; invokes v19 read-only fingerprint validation. |
| **game_agent_capture** | Requests 2–32 frames of ONE selected window; returns GPU-computed COCO confidence metadata after **native local YES** consent. |
| **game_agent_gpu_selftest** | Runs 2–32 frames on the worker's own temporary Win32 fixture; requires separate opt-in. |
| **game_agent_open_vocab** *(unreleased v22 research)* | Optional 2–12 frame open-vocabulary metadata on a separately installed private GPU runtime. Requires distinct local Universal Vision permission, existing capture permission, exact HWND/PID/start and physical native consent. No OCR, VLM, screenshot output or game actions. |

All tools are scoped to the authenticated user's own paired device. Capture
is **not** annotated read-only, despite never sending physical input, because
it requests access to window content.

### Consent and identity policy

The caller must supply **all three values returned by a fresh target scan**:
HWND, PID, and process_start_ticks. The bridge refreshes the list for a strict
match; v19 verifies the full process/window identity again before and during
processing. There is **no fallback to window index zero, foreground
application or guessed PID**.

A real window capture also requires the local v19 native TaskDialog to
display, and a person at that PC to click **Yes**. Default No and timeout
deny; a grant expires within 12 seconds or 32 frames. There is no persisted
grant. The remote caller cannot confirm the dialog.

The Windows worker may save a **JSON result report** locally under the
user's ASC-Game-Agent results directory. It does not save video. The bridge
transmits only a smaller, explicit **asc.game_agent.vision_result.v1**
structure: a COCO80 person/bus confidence pair for each frame and aggregate
safety metadata.

**COCO80 is not game-world understanding.** Nothing here identifies
Palworld pals, handles a controller, alters game files or makes autonomous
decisions. Separate Vision, Planner and ActionAdapter implementations and
real-scene acceptance tests are needed for that.

## Technical implementation

- Python 3.10+ stdlib only in the Hub agent and local policy manager.
- The local Windows tray controls enabled, capture and GPU-test permissions;
  the Hub has no remote consent/enable endpoint.
- Native v18/v19 invoked with fixed PowerShell **-File** arguments and
  subprocess shell disabled. No executable, model, PowerShell script or
  report path can be supplied remotely.
- The native wrappers check their extension SHA-256 manifests before
  execution. These are **integrity checks, not publisher signatures**.
  Trust and licensing review remain local responsibilities.
- Windows PowerShell 5.1 can emit UTF-8 native JSON using its own OEM console
  output code page. The bridge tries UTF-8 first, then the actual Windows
  GetConsoleOutputCP() for Cyrillic window titles.
- Timeouts, malformed responses, changed identity, missing components,
  unexpected frame data on CPU, missing explicit local opt-in or nonlocal
  consent **fail closed**.
- No VM, virtual monitor, hidden desktop switch, global SendInput,
  gaming process injection, scheduled GPU service, automatic model install
  or protected-content bypass.
- Linux Game Agent development remains paused. The generic Remote Hub and
  unrelated GUI Engine are unaffected when this optional flag is disabled.

## Test status

- Linux CI uses isolated mock workers to validate denied consent, stale
  fingerprints, metadata-only output, redaction, tool schemas and bad GPU
  reports. No capture occurs in CI.
- Windows smoke on an RTX 4060: both installed native packages verified,
  eligible windows enumerated with Cyrillic titles, native own-window
  fixture handled **8/8 frames** at **17.315 FPS** in a short run,
  full frames to CPU = 0 bytes and no input. This is **not real-game FPS**.
- **Actual external-window capture with a human local Yes click has not
  been performed in this bridge acceptance**, and is not claimed as tested.
- Game actions and closed-loop action/feedback remain unimplemented.

See also [safe Windows GUI](WINDOWS_GUI.ru.md) and
[isolated Win32 Desktop without a VM](ISOLATED_GUI.md).
