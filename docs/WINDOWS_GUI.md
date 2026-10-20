# Windows GUI automation: safe-by-default (agent 1.1.1)

ASC Remote Hub connects through the user's logged-in desktop session. **All apps on that Windows desktop share a mouse pointer and a foreground window.** No tool can make blind desktop clicks safe by restoring the cursor afterward. Use process APIs, `run_command`, MCP endpoints, or filesystem tools whenever possible.

## Non-invasive workflow

1. `window_list` provides the target **HWND, PID and window title**.
2. `window_controls` enumerates that window's UI Automation elements without sending pointer/keyboard events or foregrounding the window. Pass HWND, `expected_pid`, and a stable `title_contains` fragment. Use bounded `limit` and `depth`.
3. `window_action` addresses **exactly one** element by `automation_id` or exact `name`, optionally combined with `control_type` and `class_name`. Supported methods: `invoke`, `set_value`, `toggle`, `select`, `expand`, `collapse`. Ambiguous/missing elements and password fields are rejected.

Example arguments for `window_controls` (device ID, HWND and PID are illustrative):

```json
{
  "device": "d_<from-list_devices>",
  "hwnd": 123456,
  "expected_pid": 4242,
  "title_contains": "Application",
  "limit": 70,
  "depth": 6
}
```

Example `window_action` after inspecting a control:

```json
{
  "device": "d_<from-list_devices>",
  "hwnd": 123456,
  "expected_pid": 4242,
  "title_contains": "Application",
  "selector": {
    "automation_id": "searchInput",
    "control_type": "Edit"
  },
  "action": "set_value",
  "value": "Search text"
}
```

The agent **never automatically substitutes a mouse click or keyboard injection** for a failed UI Automation call.

## Active-user guard

Even UI Automation providers can activate their application as a side effect. This was confirmed during WPF testing. Therefore:

- `window_action` refuses mutations if the Windows desktop registered user input within the previous **45 seconds**. This is checked in Python and again immediately before the operation in PowerShell.
- If the foreground changes during an action, the agent returns `UIA_FOCUS_INTERFERENCE` and blocks that HWND/PID for the remainder of the agent session. The action may already have occurred, so inspect application state before retrying.
- `window_controls` is read-only and has no idle-time requirement.
- OS/UIA providers may ignore requests, lack patterns, or activate themselves. **Background operation is not universally guaranteed.** For truly simultaneous human+agent GUI work, use a separate Windows session/desktop or an application's automation API.

Administrators can override the inactivity guard in the **local agent environment** using `ASC_RC_UIA_ALLOW_ACTIVE_USER=1`, but doing so can disturb the logged-in person. This override is off by default and is not an MCP tool parameter.

## Legacy shared-desktop controls

`click_mouse`, `type_text`, `press_keys`, `scroll_mouse`, `terminal_send`, and `window_focus` are **disabled by default on Windows**, even when `ASC_RC_ALLOW_GUI=1`.

To enable a legacy action, the administrator must locally set `ASC_RC_ALLOW_INTERACTIVE_GUI=1` in the agent environment. The action must explicitly include `allow_desktop_interference=true`, plus exact HWND, PID and title. Keyboard/mouse injection also requires that the target is **already the foreground window**. Clicks must lie within its bounds; scrolling requires the mouse to already be inside it. No automatic Alt-based foreground stealing remains.

Do not enable this on a computer someone is actively using without their agreement.

## Known limitations

- Electron/Chromium, games, terminals and custom-rendered UIs may expose only panes or no actionable UI Automation patterns. Do not substitute guessed screen clicks.
- `window_screenshot` is a **screen crop** of a window's rectangle, not an occlusion-free window render. It can include overlapping windows.
- `window_action` does not guarantee that the application's *business operation* succeeded. `set_value` verifies the field value where supported. Other actions report dispatch and need a read-only state check.
- Commands launched with `run_command` are governed by OS permissions, not these GUI-specific controls. General executable access is a trust boundary.
- UI Automation requires an interactive Windows user session; Linux desktop control is a different implementation.

## Upgrade and testing

The public installer command remains stable: `npx --yes https://hub.example.com/cli.tgz`. Release **1.1.1** includes `client/windows_uia.ps1`, `client/bridge.py` and `client/asc_remote_commander.py`.

The Windows GUI safety suite is `tests/test_gui_safety.py`. After upgrading, verify `machine_info.agent_version` is `1.1.1` and refresh the MCP client's tool catalog. Never test legacy clicks on an occupied user's desktop.
