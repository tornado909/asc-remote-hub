# ASC Remote Hub: background Windows UI Automation backend.
# Windows PowerShell 5.1 + built-in .NET UIAutomationClient; NO mouse or keyboard injection.
# Input: one UTF-8 JSON request on stdin. Output: one UTF-8 JSON response on stdout.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8

function Reply([hashtable]$record) {
    $json = ConvertTo-Json -InputObject $record -Compress -Depth 9
    # Windows PowerShell 5.1 may retain the original OEM Console.Out writer.
    # Write explicit UTF-8 bytes so Unicode UIA control names remain intact.
    $blob = $utf8.GetBytes($json + [Environment]::NewLine)
    $stream = [Console]::OpenStandardOutput()
    $stream.Write($blob, 0, $blob.Length)
    $stream.Flush()
}

try {
    $reader = New-Object System.IO.StreamReader([Console]::OpenStandardInput(), $utf8)
    $raw = $reader.ReadToEnd()
    if ([string]::IsNullOrWhiteSpace($raw) -or $raw.Length -gt 16000) {
        throw 'INVALID_REQUEST'
    }
    $request = ConvertFrom-Json -InputObject $raw -ErrorAction Stop
    if ($null -eq $request) {
        throw 'INVALID_REQUEST'
    }

    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
    $pinvoke = @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class AscRemoteUiaWin32 {
    [StructLayout(LayoutKind.Sequential)]
    public struct POINT { public int X; public int Y; }
    [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr hwnd);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hwnd);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
    [DllImport("user32.dll", CharSet=CharSet.Unicode, EntryPoint="GetWindowTextW")]
    public static extern int GetWindowText(IntPtr hwnd, StringBuilder title, int capacity);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern bool GetCursorPos(out POINT point);
    [StructLayout(LayoutKind.Sequential)]
    public struct LASTINPUTINFO { public uint cbSize; public uint dwTime; }
    [DllImport("user32.dll")] public static extern bool GetLastInputInfo(ref LASTINPUTINFO info);
    [DllImport("kernel32.dll")] public static extern uint GetTickCount();
}
'@
    Add-Type -TypeDefinition $pinvoke

    [long]$numericHwnd = $request.hwnd
    [uint32]$expectedPid = $request.expected_pid
    $expectedTitle = [string]$request.title_contains
    if ($numericHwnd -le 0 -or $expectedPid -le 0 -or $expectedTitle.Trim().Length -lt 3) {
        throw 'WINDOW_IDENTITY_REQUIRED'
    }
    $handle = [IntPtr]::new($numericHwnd)
    if (-not [AscRemoteUiaWin32]::IsWindow($handle)) { throw 'WINDOW_NOT_FOUND' }
    [uint32]$actualPid = 0
    [void][AscRemoteUiaWin32]::GetWindowThreadProcessId($handle, [ref]$actualPid)
    if ($actualPid -ne $expectedPid) { throw 'WINDOW_PID_CHANGED' }
    $title = New-Object System.Text.StringBuilder(4096)
    [void][AscRemoteUiaWin32]::GetWindowText($handle, $title, $title.Capacity)
    if ($title.ToString().IndexOf($expectedTitle, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
        throw 'WINDOW_TITLE_CHANGED'
    }
    if (-not [AscRemoteUiaWin32]::IsWindowVisible($handle) -or [AscRemoteUiaWin32]::IsIconic($handle)) {
        throw 'WINDOW_NOT_VISIBLE'
    }
    $root = [System.Windows.Automation.AutomationElement]::FromHandle($handle)
    if ($null -eq $root -or [int]$root.Current.ProcessId -ne $expectedPid) {
        throw 'AUTOMATION_TARGET_CHANGED'
    }

    function Snapshot {
        $point = New-Object AscRemoteUiaWin32+POINT
        [void][AscRemoteUiaWin32]::GetCursorPos([ref]$point)
        return @{
            x = [int]$point.X
            y = [int]$point.Y
            foreground = [long][AscRemoteUiaWin32]::GetForegroundWindow().ToInt64()
        }
    }

    function ControlType($element) {
        return [string]$element.Current.ControlType.ProgrammaticName -replace '^ControlType\.', ''
    }

    function Info($element, [int]$depth) {
        $c = $element.Current
        if ([int]$c.ProcessId -ne $expectedPid) { return $null }
        [bool]$password = [bool]$c.IsPassword
        $name = if ($password) { '<protected>' } else { [string]$c.Name }
        if ($name.Length -gt 145) { $name = $name.Substring(0, 145) }
        $id = [string]$c.AutomationId
        if ($id.Length -gt 128) { $id = $id.Substring(0, 128) }
        $kind = ControlType $element
        $rect = $c.BoundingRectangle
        return @{
            name = $name
            automation_id = $id
            control_type = $kind
            class_name = [string]$c.ClassName
            enabled = [bool]$c.IsEnabled
            offscreen = [bool]$c.IsOffscreen
            password = $password
            depth = $depth
            bounds = @{
                left = [int]$rect.Left
                top = [int]$rect.Top
                width = [int]$rect.Width
                height = [int]$rect.Height
            }
        }
    }

    function Pattern($element, $which) {
        $pattern = $null
        $supported = $element.TryGetCurrentPattern($which, [ref]$pattern)
        if (-not $supported -or $null -eq $pattern) { throw 'CONTROL_PATTERN_UNSUPPORTED' }
        return $pattern
    }

    $before = Snapshot
    switch ([string]$request.mode) {
        'list' {
            [int]$limit = $request.limit
            [int]$maxDepth = $request.depth
            if ($limit -lt 1 -or $limit -gt 150 -or $maxDepth -lt 1 -or $maxDepth -gt 9) {
                throw 'INVALID_LIST_BOUNDS'
            }
            $queue = New-Object System.Collections.Queue
            $queue.Enqueue(@{ element=$root; depth=0 })
            $list = New-Object System.Collections.ArrayList
            [int]$visited = 0
            [bool]$truncated = $false
            while ($queue.Count -gt 0) {
                $item = $queue.Dequeue()
                if ($item.depth -ge $maxDepth) { continue }
                $children = $item.element.FindAll(
                    [System.Windows.Automation.TreeScope]::Children,
                    [System.Windows.Automation.Condition]::TrueCondition)
                foreach ($child in $children) {
                    $visited++
                    if ($visited -gt 2500) { $truncated = $true; break }
                    try {
                        $detail = Info $child ($item.depth+1)
                        if ($null -ne $detail) {
                            if (($detail.name -or $detail.automation_id) -and $list.Count -lt $limit) {
                                [void]$list.Add($detail)
                            }
                            if ($list.Count -ge $limit) { $truncated = $true; break }
                            $queue.Enqueue(@{ element=$child; depth=($item.depth+1) })
                        }
                    } catch {
                        # UIA providers may destroy controls while enumerating.
                        continue
                    }
                }
                if ($truncated) { break }
            }
            $after = Snapshot
            Reply @{
                ok = $true
                mode = 'background'
                hwnd = $numericHwnd
                pid = $expectedPid
                controls = @($list.ToArray())
                visited = $visited
                truncated = $truncated
                cursor_unchanged = ($before.x -eq $after.x -and $before.y -eq $after.y)
                foreground_unchanged = ($before.foreground -eq $after.foreground)
            }
        }
        'action' {
            $selector = $request.selector
            if ($null -eq $selector) { throw 'CONTROL_SELECTOR_REQUIRED' }
            $autoId = [string]$selector.automation_id
            $name = [string]$selector.name
            $kind = [string]$selector.control_type
            $className = [string]$selector.class_name
            if (-not $autoId.Trim() -and -not $name.Trim()) {
                throw 'CONTROL_SELECTOR_REQUIRED'
            }
            if ($autoId.Length -gt 128 -or $name.Length -gt 180 -or $kind.Length -gt 64 -or $className.Length -gt 128) {
                throw 'CONTROL_SELECTOR_INVALID'
            }

            if ($autoId) {
                $condition = New-Object System.Windows.Automation.PropertyCondition(
                    [System.Windows.Automation.AutomationElement]::AutomationIdProperty, $autoId)
            } else {
                $condition = New-Object System.Windows.Automation.PropertyCondition(
                    [System.Windows.Automation.AutomationElement]::NameProperty, $name)
            }
            $found = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition)
            if ($found.Count -gt 300) { throw 'CONTROL_SELECTOR_TOO_BROAD' }
            $matches = New-Object System.Collections.ArrayList
            foreach ($element in $found) {
                try {
                    $current = $element.Current
                    if ($current.ProcessId -ne $expectedPid) { continue }
                    if ($autoId -and $current.AutomationId -cne $autoId) { continue }
                    if ($name -and $current.Name -cne $name) { continue }
                    if ($kind -and (ControlType $element) -cne $kind) { continue }
                    if ($className -and $current.ClassName -cne $className) { continue }
                    [void]$matches.Add($element)
                } catch {
                    continue
                }
            }
            if ($matches.Count -eq 0) { throw 'CONTROL_NOT_FOUND' }
            if ($matches.Count -gt 1) { throw 'CONTROL_AMBIGUOUS' }
            $target = $matches[0]
            $current = $target.Current
            if (-not $current.IsEnabled) { throw 'CONTROL_DISABLED' }
            if ($current.IsOffscreen) { throw 'CONTROL_OFFSCREEN' }
            if ($current.IsPassword) { throw 'SECURE_INPUT_REFUSED' }

            # Check again immediately before mutation; UIA providers are dynamic.
            if (-not [AscRemoteUiaWin32]::IsWindow($handle)) { throw 'WINDOW_DISAPPEARED' }
            [uint32]$recheckPid = 0
            [void][AscRemoteUiaWin32]::GetWindowThreadProcessId($handle, [ref]$recheckPid)
            if ($recheckPid -ne $expectedPid) { throw 'WINDOW_PID_CHANGED' }
            $freshTitle = New-Object System.Text.StringBuilder(4096)
            [void][AscRemoteUiaWin32]::GetWindowText($handle,$freshTitle,$freshTitle.Capacity)
            if ($freshTitle.ToString().IndexOf($expectedTitle,[StringComparison]::OrdinalIgnoreCase) -lt 0) {
                throw 'WINDOW_TITLE_CHANGED'
            }

            # Never mutate controls on a desktop actively used by its owner.
            [int]$minimumIdle = $request.min_idle_seconds
            if ($minimumIdle -lt 0 -or $minimumIdle -gt 3600) { throw 'INVALID_IDLE_THRESHOLD' }
            if ($minimumIdle -gt 0) {
                $last = New-Object AscRemoteUiaWin32+LASTINPUTINFO
                $last.cbSize = [Runtime.InteropServices.Marshal]::SizeOf([type][AscRemoteUiaWin32+LASTINPUTINFO])
                if (-not [AscRemoteUiaWin32]::GetLastInputInfo([ref]$last)) { throw 'USER_IDLE_CHECK_FAILED' }
                [uint32]$nowTicks = [AscRemoteUiaWin32]::GetTickCount()
                $elapsed = ([uint64]$nowTicks + 4294967296 - [uint64]$last.dwTime) % 4294967296
                if ($elapsed -lt ([uint64]$minimumIdle*1000)) { throw 'USER_ACTIVE_REFUSED' }
            }
            # Capture metadata before Invoke: applications may destroy or
            # replace their controls during the action.
            $preName = [string]$current.Name
            if ($preName.Length -gt 145) { $preName = $preName.Substring(0,145) }
            $preId = [string]$current.AutomationId
            $preKind = ControlType $target
            $action = [string]$request.action
            $verified = $false
            switch ($action) {
                'invoke' {
                    $pat = Pattern $target ([System.Windows.Automation.InvokePattern]::Pattern)
                    $pat.Invoke()
                }
                'set_value' {
                    if ($null -eq $request.value -or $request.value -isnot [string]) {
                        throw 'VALUE_REQUIRED'
                    }
                    if ($request.value.Length -gt 4000) { throw 'VALUE_TOO_LONG' }
                    $pat = Pattern $target ([System.Windows.Automation.ValuePattern]::Pattern)
                    if ($pat.Current.IsReadOnly) { throw 'CONTROL_READ_ONLY' }
                    $pat.SetValue([string]$request.value)
                    try { $verified = ($pat.Current.Value -ceq [string]$request.value) }
                    catch { $verified = $false }
                }
                'toggle' {
                    $pat = Pattern $target ([System.Windows.Automation.TogglePattern]::Pattern)
                    $pat.Toggle()
                }
                'select' {
                    $pat = Pattern $target ([System.Windows.Automation.SelectionItemPattern]::Pattern)
                    $pat.Select()
                }
                'expand' {
                    $pat = Pattern $target ([System.Windows.Automation.ExpandCollapsePattern]::Pattern)
                    $pat.Expand()
                }
                'collapse' {
                    $pat = Pattern $target ([System.Windows.Automation.ExpandCollapsePattern]::Pattern)
                    $pat.Collapse()
                }
                default { throw 'UNKNOWN_CONTROL_ACTION' }
            }
            $after = Snapshot
            Reply @{
                ok = $true
                hwnd = $numericHwnd
                pid = $expectedPid
                mode = 'background'
                action = $action
                matched_control = @{
                    name = $preName
                    automation_id = $preId
                    control_type = $preKind
                }
                dispatched = $true
                value_verified = $verified
                # Invoke / select effects belong to the app and cannot be proven by the API.
                action_effect_verified = ($action -eq 'set_value' -and $verified)
                cursor_unchanged = ($before.x -eq $after.x -and $before.y -eq $after.y)
                foreground_unchanged = ($before.foreground -eq $after.foreground)
            }
        }
        default { throw 'UNKNOWN_UIA_MODE' }
    }
}
catch {
    $message = [string]$_.Exception.Message
    $known = @(
        'INVALID_REQUEST','WINDOW_IDENTITY_REQUIRED','WINDOW_NOT_FOUND',
        'WINDOW_PID_CHANGED','WINDOW_TITLE_CHANGED','WINDOW_NOT_VISIBLE',
        'AUTOMATION_TARGET_CHANGED','INVALID_LIST_BOUNDS',
        'CONTROL_SELECTOR_REQUIRED','CONTROL_SELECTOR_INVALID',
        'CONTROL_SELECTOR_TOO_BROAD','CONTROL_NOT_FOUND','CONTROL_AMBIGUOUS',
        'CONTROL_DISABLED','CONTROL_OFFSCREEN','SECURE_INPUT_REFUSED',
        'WINDOW_DISAPPEARED','VALUE_REQUIRED','VALUE_TOO_LONG',
        'CONTROL_READ_ONLY','UNKNOWN_CONTROL_ACTION','UNKNOWN_UIA_MODE',
        'CONTROL_PATTERN_UNSUPPORTED','INVALID_IDLE_THRESHOLD','USER_IDLE_CHECK_FAILED','USER_ACTIVE_REFUSED'
    )
    $code = if ($known -contains $message) { $message } else { 'UIA_PROVIDER_ERROR' }
    Reply @{ ok = $false; error = $code; mode = 'background'; cursor_injected = $false }
}
