# ASC Remote Hub v0.3: enroll this Windows PC under your OWN hub account.
# No OpenAI Platform key. No Desktop Commander. No open listening ports.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$HubUrl,
    [string]$DeviceName=$env:COMPUTERNAME,
    [string]$Roots=$env:USERPROFILE,
    [switch]$FullAccess,
    [switch]$AllowGui,
    [switch]$SkipEnrollment,
    [switch]$NoScheduledTask
)
$ErrorActionPreference='Stop'
$venvPython=Join-Path $PSScriptRoot '..\.venv\Scripts\python.exe'
$python=if(Test-Path -LiteralPath $venvPython){(Resolve-Path $venvPython).Path}else{(Get-Command python.exe -ErrorAction Stop).Source}
$client=(Resolve-Path (Join-Path $PSScriptRoot '..\client\bridge.py')).Path
$config=Join-Path $env:APPDATA 'ASC-Remote-Hub\agent.json'
# Pairing code entered INSIDE Python with hidden echo; never in process arguments.
$argsList=@($client,'enroll','--hub',$HubUrl,'--name',$DeviceName,'--config',$config,'--roots')
$argsList+=@($Roots -split ';')
if($FullAccess){$argsList+=@('--write','--exec','--gui')}
elseif($AllowGui){$argsList+='--gui'}
if(-not $SkipEnrollment){
    & $python @argsList
    if($LASTEXITCODE -ne 0){throw 'Device enrollment failed'}
}else{
    if(-not(Test-Path -LiteralPath $config)){throw 'Cannot restore task: no saved device credential'}
    Write-Host 'Existing enrollment preserved; restoring the background task.'
}
$taskName='ASC Remote Hub Agent'
if(-not $NoScheduledTask){
    $starter=(Resolve-Path (Join-Path $PSScriptRoot 'launch_tray_windows.vbs')).Path
    $trayScript=(Resolve-Path (Join-Path $PSScriptRoot 'tray_windows.ps1')).Path
    $wscript=Join-Path $env:SystemRoot 'System32\wscript.exe'
    if (-not (Test-Path -LiteralPath $wscript)) { throw 'Windows Script Host is required for the tray launcher.' }

    $action=New-ScheduledTaskAction -Execute $wscript -Argument ('//B //NoLogo "{0}"' -f $starter)
    $trigger=New-ScheduledTaskTrigger -AtLogOn -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
    # The icon and GUI functions require an actual interactive desktop session.
    $principal=New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
    $settings=New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

    $oldTask=Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($oldTask -and $oldTask.State -eq 'Running') {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction Stop
        Start-Sleep -Seconds 1
    }
    # Stopping wscript.exe can leave its PowerShell + Python descendants alive.
    # Kill ONLY supervisors executing this specific installed tray script.
    foreach($process in @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue)){
        if ($process.CommandLine -and $process.CommandLine.IndexOf($trayScript,[StringComparison]::OrdinalIgnoreCase) -ge 0) {
            try {
                & (Join-Path $env:SystemRoot 'System32\taskkill.exe') /PID $process.ProcessId /T /F 2>$null | Out-Null
            } catch {}
        }
    }
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null

    # A registered task is NOT proof of a running or authenticated agent.
    # The Python worker writes agent.ready.json only after /agent/hello succeeds.
    $installStarted=[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
    $ready=Join-Path $env:LOCALAPPDATA 'ASC-Remote-Hub\logs\agent.ready.json'
    $deviceId=(Get-Content -LiteralPath $config -Raw -Encoding UTF8 | ConvertFrom-Json).device_id
    Start-ScheduledTask -TaskName $taskName -ErrorAction Stop
    Write-Host 'Ожидаю подтверждения подключения от сервера (до 40 секунд)...'
    $connected=$false
    $deadline=(Get-Date).AddSeconds(40)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 1200
        if (Test-Path -LiteralPath $ready) {
            try {
                $report=Get-Content -LiteralPath $ready -Raw -Encoding UTF8 | ConvertFrom-Json
                $pidValue=[int]$report.pid
                if ($report.device_id -eq $deviceId -and
                    [double]$report.connected_at -ge ($installStarted - 1.0) -and
                    (Get-Process -Id $pidValue -ErrorAction SilentlyContinue)) {
                    $connected=$true
                    break
                }
            } catch {}
        }
        $status=(Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue).State
        if ($status -eq 'Ready' -and ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()/1000.0-$installStarted) -gt 5) {
            break
        }
    }
    if (-not $connected) {
        $status=(Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue).State
        $trailog=Join-Path $env:LOCALAPPDATA 'ASC-Remote-Hub\logs\tray-supervisor.log'
        $agentlog=Join-Path $env:LOCALAPPDATA 'ASC-Remote-Hub\logs\agent.stderr.log'
        Write-Warning "Устройство успешно зарегистрировано, но агент пока не подтвердил подключение. Состояние задания: $status"
        Write-Host "Журнал трея: $trailog"
        Write-Host "Журнал Python: $agentlog"
        if (Test-Path -LiteralPath $trailog) {
            Get-Content -LiteralPath $trailog -Tail 8 -ErrorAction SilentlyContinue | ForEach-Object {Write-Host $_}
        }
        if (Test-Path -LiteralPath $agentlog) {
            Get-Content -LiteralPath $agentlog -Tail 8 -ErrorAction SilentlyContinue | ForEach-Object {Write-Host $_}
        }
        throw 'Автозапуск не подтвердился. Существующая привязка сохранена; повторите установку без нового кода.'
    }
    Write-Host "✓ Сервер подтвердил подключение устройства $deviceId."
    Write-Host "Tray agent started: $taskName. Use the ASC icon in the Windows notification area."
}else{
    Write-Host "Manual start: powershell -File '$PSScriptRoot\start_windows.ps1'"
}
Write-Host 'Windows device credentials preserved with current-user DPAPI.'
