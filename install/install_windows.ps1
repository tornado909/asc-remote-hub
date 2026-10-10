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
    $starter=(Resolve-Path (Join-Path $PSScriptRoot 'start_windows.ps1')).Path
    $action=New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}"' -f $starter)
    $trigger=New-ScheduledTaskTrigger -AtLogOn -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
    $settings=New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName $taskName
    Write-Host "Agent task started: $taskName. Check the hub dashboard for online status."
}else{
    Write-Host "Manual start: powershell -File `"$PSScriptRoot\start_windows.ps1`""
}
Write-Host 'Windows device token encrypted for current user using DPAPI.'
