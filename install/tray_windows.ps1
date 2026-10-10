# ASC Remote Hub Windows tray supervisor. Runs in STA Windows PowerShell,
# hidden by launch_tray_windows.vbs. No external tray packages required.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$Config = Join-Path $env:APPDATA 'ASC-Remote-Hub\agent.json'
if (-not (Test-Path -LiteralPath $Config)) { throw "Device is not enrolled: $Config" }
$VenvPython = Join-Path $PSScriptRoot '..\.venv\Scripts\python.exe'
$Python = if (Test-Path -LiteralPath $VenvPython) {
    (Resolve-Path -LiteralPath $VenvPython).Path
} else { (Get-Command python.exe -ErrorAction Stop).Source }
$Worker = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\client\bridge.py')).Path
$Logs = Join-Path $env:LOCALAPPDATA 'ASC-Remote-Hub\logs'
New-Item -ItemType Directory -Path $Logs -Force | Out-Null
$OutLog = Join-Path $Logs 'agent.stdout.log'
$ErrLog = Join-Path $Logs 'agent.stderr.log'
$script:HubUrl = (Get-Content -LiteralPath $Config -Raw -Encoding UTF8 | ConvertFrom-Json).hub_url
if (-not ($script:HubUrl -match '^https://[^/]+/?
$script:Stopping = $false
$script:Tray = New-Object System.Windows.Forms.NotifyIcon
$script:Tray.Icon = [System.Drawing.SystemIcons]::Application
$script:Tray.Text = 'ASC Remote Hub Agent'
$Menu = New-Object System.Windows.Forms.ContextMenuStrip
$OpenItem = $Menu.Items.Add('Открыть хаб')
$RestartItem = $Menu.Items.Add('Перезапустить агент')
$Menu.Items.Add('-') | Out-Null
$ExitItem = $Menu.Items.Add('Выход')
$script:Tray.ContextMenuStrip = $Menu

function Start-AgentWorker {
    if ($script:Stopping) { return }
    if ($null -ne $script:WorkerProcess) {
        try { if (-not $script:WorkerProcess.HasExited) { return } } catch {}
    }
    $script:LastLaunch = [datetime]::UtcNow
    # A redirected, hidden process has no visible console and preserves logs.
    $args = '"{0}" run --config "{1}"' -f $Worker, $Config
    $script:WorkerProcess = Start-Process -FilePath $Python -ArgumentList $args -WindowStyle Hidden -RedirectStandardOutput $OutLog -RedirectStandardError $ErrLog -PassThru
    if ($null -eq $script:WorkerProcess) { throw 'Failed to start agent worker' }
    $script:Tray.Text = 'ASC Remote Hub Agent (работает)'
}
function Stop-AgentWorker {
    $Process = $script:WorkerProcess
    $script:WorkerProcess = $null
    if ($null -eq $Process) { return }
    try {
        if (-not $Process.HasExited) {
            # Python venv launcher can spawn a child process; stop the process tree.
            $PidValue = [int]$Process.Id
            & "$env:SystemRoot\System32\taskkill.exe" /PID $PidValue /T /F 2>$null | Out-Null
            try { $Process.WaitForExit(4000) | Out-Null } catch {}
        }
    } catch {}
    try { $Process.Dispose() } catch {}
}

$OpenItem.add_Click({ Start-Process $script:HubUrl })
$RestartItem.add_Click({
    Stop-AgentWorker
    try {
        Start-AgentWorker
    } catch {
        $script:Tray.Text = 'ASC Remote Hub Agent (ошибка)'
    }
})
$ExitItem.add_Click({
    $script:Stopping = $true
    $script:Tray.Visible = $false
    Stop-AgentWorker
    $script:Context.ExitThread()
})

$Timer = New-Object System.Windows.Forms.Timer
$Timer.Interval = 10000
$Timer.add_Tick({
    if ($script:Stopping) { return }
    $Alive = $false
    try { $Alive = $null -ne $script:WorkerProcess -and -not $script:WorkerProcess.HasExited } catch {}
    if (-not $Alive) {
        $script:Tray.Text = 'ASC Remote Hub Agent (перезапуск)'
        if (([datetime]::UtcNow - $script:LastLaunch).TotalSeconds -ge 30) {
            try { Start-AgentWorker } catch {}
        }
    }
})

$script:Context = New-Object System.Windows.Forms.ApplicationContext
try {
    Start-AgentWorker
    $script:Tray.Visible = $true
    $Timer.Start()
    [System.Windows.Forms.Application]::Run($script:Context)
} finally {
    $script:Stopping = $true
    $Timer.Stop()
    Stop-AgentWorker
    $script:Tray.Visible = $false
    $Timer.Dispose()
    $Menu.Dispose()
    $script:Tray.Dispose()
    $script:Context.Dispose()
}
)) { throw 'Invalid hub URL in agent configuration' }
$script:WorkerProcess = $null
$script:LastLaunch = [datetime]::MinValue
$script:Stopping = $false
$script:Tray = New-Object System.Windows.Forms.NotifyIcon
$script:Tray.Icon = [System.Drawing.SystemIcons]::Application
$script:Tray.Text = 'ASC Remote Hub Agent'
$Menu = New-Object System.Windows.Forms.ContextMenuStrip
$OpenItem = $Menu.Items.Add('Открыть хаб')
$RestartItem = $Menu.Items.Add('Перезапустить агент')
$Menu.Items.Add('-') | Out-Null
$ExitItem = $Menu.Items.Add('Выход')
$script:Tray.ContextMenuStrip = $Menu

function Start-AgentWorker {
    if ($script:Stopping) { return }
    if ($null -ne $script:WorkerProcess) {
        try { if (-not $script:WorkerProcess.HasExited) { return } } catch {}
    }
    $script:LastLaunch = [datetime]::UtcNow
    $StartInfo = New-Object System.Diagnostics.ProcessStartInfo
    $StartInfo.FileName = $Python
    $StartInfo.Arguments = ('"{0}" run --config "{1}"' -f $Worker, $Config)
    $StartInfo.UseShellExecute = $false
    $StartInfo.CreateNoWindow = $true
    $StartInfo.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
    $StartInfo.RedirectStandardOutput = $true
    $StartInfo.RedirectStandardError = $true
    # Asynchronous readers prevent filled pipes from blocking the worker.
    $Process = New-Object System.Diagnostics.Process
    $Process.StartInfo = $StartInfo
    $Process.EnableRaisingEvents = $false
    if (-not $Process.Start()) { throw 'Failed to start remote agent worker.' }
    $Process.BeginOutputReadLine()
    $Process.BeginErrorReadLine()
    $script:WorkerProcess = $Process
    $script:Tray.Text = 'ASC Remote Hub Agent (работает)'
}
function Stop-AgentWorker {
    $Process = $script:WorkerProcess
    $script:WorkerProcess = $null
    if ($null -eq $Process) { return }
    try {
        if (-not $Process.HasExited) {
            # Python venv launcher can spawn a child process; stop the process tree.
            $PidValue = [int]$Process.Id
            & "$env:SystemRoot\System32\taskkill.exe" /PID $PidValue /T /F 2>$null | Out-Null
            try { $Process.WaitForExit(4000) | Out-Null } catch {}
        }
    } catch {}
    try { $Process.Dispose() } catch {}
}

$OpenItem.add_Click({ Start-Process 'https://remote.ascdev.ru/' })
$RestartItem.add_Click({
    Stop-AgentWorker
    try {
        Start-AgentWorker
    } catch {
        $script:Tray.Text = 'ASC Remote Hub Agent (ошибка)'
    }
})
$ExitItem.add_Click({
    $script:Stopping = $true
    $script:Tray.Visible = $false
    Stop-AgentWorker
    $script:Context.ExitThread()
})

$Timer = New-Object System.Windows.Forms.Timer
$Timer.Interval = 10000
$Timer.add_Tick({
    if ($script:Stopping) { return }
    $Alive = $false
    try { $Alive = $null -ne $script:WorkerProcess -and -not $script:WorkerProcess.HasExited } catch {}
    if (-not $Alive) {
        $script:Tray.Text = 'ASC Remote Hub Agent (перезапуск)'
        if (([datetime]::UtcNow - $script:LastLaunch).TotalSeconds -ge 30) {
            try { Start-AgentWorker } catch {}
        }
    }
})

$script:Context = New-Object System.Windows.Forms.ApplicationContext
try {
    Start-AgentWorker
    $script:Tray.Visible = $true
    $Timer.Start()
    [System.Windows.Forms.Application]::Run($script:Context)
} finally {
    $script:Stopping = $true
    $Timer.Stop()
    Stop-AgentWorker
    $script:Tray.Visible = $false
    $Timer.Dispose()
    $Menu.Dispose()
    $script:Tray.Dispose()
    $script:Context.Dispose()
}
