# ASC Remote Hub Windows tray supervisor. STA Windows PowerShell,
# launched hidden through launch_tray_windows.vbs (no console window).
$ErrorActionPreference = 'Stop'
$script:TrayDiagnostics = Join-Path $env:LOCALAPPDATA 'ASC-Remote-Hub\logs\tray-supervisor.log'
function Write-TrayDiagnostic {
    param([string]$Message)
    try {
        $directory = Split-Path -Parent $script:TrayDiagnostics
        New-Item -ItemType Directory -Path $directory -Force -ErrorAction Stop | Out-Null
        $line = ([datetime]::UtcNow.ToString('o')) + ' ' + ($Message -replace '[\r\n]+',' ')
        Add-Content -LiteralPath $script:TrayDiagnostics -Value $line -Encoding UTF8 -ErrorAction Stop
    } catch {
        # The supervisor must never die merely because diagnostic logging failed.
    }
}
trap {
    $errorText = $_.Exception.GetType().Name + ': ' + $_.Exception.Message
    Write-TrayDiagnostic ('FATAL line ' + $_.InvocationInfo.ScriptLineNumber + ' ' + $errorText)
    try { [Console]::Error.WriteLine('ASC Remote Hub tray failed: ' + $errorText) } catch {}
    exit 1
}
Write-TrayDiagnostic 'START tray supervisor'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$Config = Join-Path $env:APPDATA 'ASC-Remote-Hub\agent.json'
if (-not (Test-Path -LiteralPath $Config)) { throw "Device is not enrolled: $Config" }
$VenvPython = Join-Path $PSScriptRoot '..\.venv\Scripts\python.exe'
$Python = if (Test-Path -LiteralPath $VenvPython) {
    (Resolve-Path -LiteralPath $VenvPython).Path
} else {
    (Get-Command python.exe -ErrorAction Stop).Source
}
$Worker = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\client\bridge.py')).Path
$Logs = Join-Path $env:LOCALAPPDATA 'ASC-Remote-Hub\logs'
New-Item -ItemType Directory -Path $Logs -Force | Out-Null
$OutLog = Join-Path $Logs 'agent.stdout.log'
$ErrLog = Join-Path $Logs 'agent.stderr.log'
$script:HubUrl = (Get-Content -LiteralPath $Config -Raw -Encoding UTF8 | ConvertFrom-Json).hub_url
$ParsedHub = [System.Uri]$script:HubUrl
if ($ParsedHub.Scheme -ne 'https' -or $ParsedHub.AbsolutePath -ne '/') {
    throw 'Invalid HTTPS hub URL in agent configuration'
}
$script:WorkerProcess = $null
$script:LastLaunch = [datetime]::MinValue
$script:Stopping = $false
$script:Tray = New-Object System.Windows.Forms.NotifyIcon
$script:TrayIconResource = $null
$script:TrayIconPath = Join-Path $PSScriptRoot 'asc-remote-hub.ico'
if (Test-Path -LiteralPath $script:TrayIconPath) {
    try {
        $script:TrayIconResource = [System.Drawing.Icon]::new($script:TrayIconPath)
        $script:Tray.Icon = $script:TrayIconResource
    } catch {
        Write-TrayDiagnostic ('WARNING icon fallback: ' + $_.Exception.GetType().Name)
        $script:Tray.Icon = [System.Drawing.SystemIcons]::Application
    }
} else {
    $script:Tray.Icon = [System.Drawing.SystemIcons]::Application
}
$script:Tray.Text = 'ASC Remote Hub Agent'
$Menu = New-Object System.Windows.Forms.ContextMenuStrip
$OpenItem = $Menu.Items.Add('Открыть хаб')
$RestartItem = $Menu.Items.Add('Перезапустить агент')
$Menu.Items.Add('-') | Out-Null
$UnpairItem = $Menu.Items.Add('Отвязать от хаба...')
$UninstallItem = $Menu.Items.Add('Удалить ASC Remote Hub...')
$Menu.Items.Add('-') | Out-Null
$ExitItem = $Menu.Items.Add('Выход')
$script:Tray.ContextMenuStrip = $Menu

function Start-AgentWorker {
    if ($script:Stopping) { return }
    if ($null -ne $script:WorkerProcess) {
        try {
            if (-not $script:WorkerProcess.HasExited) { return }
        } catch {}
    }
    $script:LastLaunch = [datetime]::UtcNow
    $args = '"{0}" run --config "{1}"' -f $Worker, $Config
    # Redirect output to files, preventing any visible worker terminal.
    Write-TrayDiagnostic 'Launching background Python worker'
    $script:WorkerProcess = Start-Process -FilePath $Python -ArgumentList $args -WindowStyle Hidden -RedirectStandardOutput $OutLog -RedirectStandardError $ErrLog -PassThru
    if ($null -eq $script:WorkerProcess) { throw 'Unable to start agent worker' }
    Write-TrayDiagnostic ('Background Python launcher started PID ' + $script:WorkerProcess.Id)
    $script:Tray.Text = 'ASC Remote Hub Agent (запускается)'
}
function Stop-AgentWorker {
    $Process = $script:WorkerProcess
    $script:WorkerProcess = $null
    if ($null -eq $Process) { return }
    try {
        if (-not $Process.HasExited) {
            # The Python venv launcher may have a Python child: stop both.
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
        Write-TrayDiagnostic ('Manual restart failed: ' + $_.Exception.GetType().Name + ': ' + $_.Exception.Message)
        $script:Tray.Text = 'ASC Remote Hub Agent (ошибка)'
    }
})
function Invoke-LocalLifecycle {
    param([ValidateSet('unpair','uninstall')][string]$Action)
    $delete = $Action -eq 'uninstall'
    $question = if ($delete) {
        'Удалить ASC Remote Hub с этого компьютера? Будут отозвана привязка, отключён автозапуск и удалены файлы агента.'
    } else {
        'Отвязать этот компьютер от текущего хаба? Агент будет остановлен, но приложение останется установленным.'
    }
    $answer=[System.Windows.Forms.MessageBox]::Show($question,'ASC Remote Hub',
        [System.Windows.Forms.MessageBoxButtons]::YesNo,
        [System.Windows.Forms.MessageBoxIcon]::Warning,
        [System.Windows.Forms.MessageBoxDefaultButton]::Button2)
    if($answer -ne [System.Windows.Forms.DialogResult]::Yes){ return }
    $manager=Join-Path $PSScriptRoot 'manage_agent.py'
    $systemPython=(Get-Command python.exe -ErrorAction Stop).Source
    $quotedPy=$systemPython.Replace("'","''")
    $quotedManager=$manager.Replace("'","''")
    $code="& '$quotedPy' '$quotedManager' $Action --yes"
    $encoded=[Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($code))
    try{
        Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') -ArgumentList @(
            '-NoProfile','-NoExit','-ExecutionPolicy','Bypass','-EncodedCommand',$encoded
        ) -ErrorAction Stop | Out-Null
        $script:Stopping=$true
        Stop-AgentWorker
        $script:Context.ExitThread()
    }catch{
        [System.Windows.Forms.MessageBox]::Show('Не удалось открыть управление агентом: '+$_.Exception.Message)
    }
}
$UnpairItem.add_Click({ Invoke-LocalLifecycle 'unpair' })
$UninstallItem.add_Click({ Invoke-LocalLifecycle 'uninstall' })
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
    try {
        $Alive = $null -ne $script:WorkerProcess -and -not $script:WorkerProcess.HasExited
    } catch {}
    if (-not $Alive) {
        $script:Tray.Text = 'ASC Remote Hub Agent (перезапуск)'
        if (([datetime]::UtcNow - $script:LastLaunch).TotalSeconds -ge 30) {
            try { Start-AgentWorker } catch {
                Write-TrayDiagnostic ('Worker restart failed: ' + $_.Exception.GetType().Name + ': ' + $_.Exception.Message)
                $script:Tray.Text = 'ASC Remote Hub Agent (ошибка)'
            }
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
    if ($null -ne $script:TrayIconResource) { $script:TrayIconResource.Dispose() }
    $script:Context.Dispose()
}
