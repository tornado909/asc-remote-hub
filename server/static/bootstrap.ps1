# ASC Remote Hub Windows bootstrap: only trusted WinGet packages.
# Works without Node.js, npm, npx or Python already installed.
# The agent installer remains the single source of truth for enrollment.
[CmdletBinding()]
param(
    [switch]$DependenciesOnly,
    [switch]$CheckOnly
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$HubOrigin = '__HUB_ORIGIN__'

if ($env:OS -ne 'Windows_NT') {
    throw 'ASC bootstrap for Windows must run in Windows PowerShell.'
}
if ($HubOrigin -notmatch '^https://(?:[A-Za-z0-9.-]+|\[[0-9a-fA-F:]+\])(?::[0-9]{1,5})?$') {
    throw 'Untrusted or invalid HTTPS Hub origin.'
}

function Refresh-ProcessPath {
    # WinGet writes new user/machine PATH values, but an already open terminal
    # does not automatically inherit them. Preserve the existing PATH first.
    $entries = New-Object 'System.Collections.Generic.List[string]'
    foreach ($value in @(
        $env:Path,
        [Environment]::GetEnvironmentVariable('Path','Machine'),
        [Environment]::GetEnvironmentVariable('Path','User'),
        (Join-Path $env:ProgramFiles 'nodejs'),
        (Join-Path $env:LOCALAPPDATA 'Programs\nodejs'),
        (Join-Path $env:APPDATA 'npm')
    )) {
        if ($value) {
            foreach ($part in ([string]$value).Split(';')) {
                if ($part -and -not $entries.Contains($part)) {
                    [void]$entries.Add($part)
                }
            }
        }
    }
    foreach ($minor in @(14,13,12,11,10)) {
        $dir = Join-Path $env:LOCALAPPDATA ("Programs\Python\Python3{0}" -f $minor)
        if ((Test-Path (Join-Path $dir 'python.exe')) -and
            -not $entries.Contains($dir)) { [void]$entries.Insert(0,$dir) }
    }
    $env:Path = $entries -join ';'
}

function Find-Python {
    $candidates = New-Object 'System.Collections.Generic.List[string]'
    foreach ($cmd in @(Get-Command python.exe -All -ErrorAction SilentlyContinue)) {
        if ($cmd -and $cmd.Source -and -not $candidates.Contains($cmd.Source)) {
            [void]$candidates.Add($cmd.Source)
        }
    }
    foreach ($minor in @(14,13,12,11,10)) {
        $path = Join-Path $env:LOCALAPPDATA ("Programs\Python\Python3{0}\python.exe" -f $minor)
        if ((Test-Path $path) -and -not $candidates.Contains($path)) {
            [void]$candidates.Add($path)
        }
    }
    foreach ($minor in @(14,13,12,11,10)) {
        $path = Join-Path $env:ProgramFiles ("Python3{0}\python.exe" -f $minor)
        if ((Test-Path $path) -and -not $candidates.Contains($path)) {
            [void]$candidates.Add($path)
        }
    }
    foreach ($candidate in $candidates) {
        try {
            $version = (& $candidate -c 'import sys; print(sys.version_info.major,sys.version_info.minor)' 2>$null | Out-String).Trim()
            if ($LASTEXITCODE -eq 0 -and $version -match '^3 ([0-9]+)$' -and
                [int]$Matches[1] -ge 10) {
                return $candidate
            }
        } catch {
            # App Execution Alias, inaccessible binary or unusable Python.
        }
    }
    return $null
}

function Find-NodeNpx {
    $nodeCandidates = New-Object 'System.Collections.Generic.List[string]'
    foreach ($cmd in @(Get-Command node.exe -All -ErrorAction SilentlyContinue)) {
        if ($cmd -and $cmd.Source -and -not $nodeCandidates.Contains($cmd.Source)) {
            [void]$nodeCandidates.Add($cmd.Source)
        }
    }
    $installed = Join-Path $env:ProgramFiles 'nodejs\node.exe'
    if ((Test-Path $installed) -and -not $nodeCandidates.Contains($installed)) {
        [void]$nodeCandidates.Add($installed)
    }
    foreach ($node in $nodeCandidates) {
        try {
            $version = (& $node --version 2>$null | Out-String).Trim()
            if ($LASTEXITCODE -ne 0 -or $version -notmatch '^v([0-9]+)\.') { continue }
            if ([int]$Matches[1] -lt 18) { continue }
            $npx = Join-Path (Split-Path -Path $node -Parent) 'npx.cmd'
            if (-not (Test-Path -LiteralPath $npx -PathType Leaf)) {
                $command = Get-Command npx.cmd -ErrorAction SilentlyContinue
                if (-not $command) { continue }
                $npx = $command.Source
            }
            $npmVersion = (& $npx --version 2>$null | Out-String).Trim()
            if ($LASTEXITCODE -eq 0 -and $npmVersion -match '^[0-9]+\.[0-9]+') {
                return @{ Node = $node; Npx = $npx; Version = $version }
            }
        } catch {
            # One broken Node installation must not hide another valid one.
        }
    }
    return $null
}

Refresh-ProcessPath
$python = Find-Python
$node = Find-NodeNpx
$tar = Get-Command tar.exe -ErrorAction SilentlyContinue
Write-Host ('Python 3.10+: ' + $(if ($python) { $python } else { 'missing' }))
Write-Host ('Node.js 18+ and npx: ' + $(if ($node) { $node.Version } else { 'missing' }))
Write-Host ('tar.exe: ' + $(if ($tar) { 'available' } else { 'missing' }))

if ($CheckOnly) {
    Write-Host 'Read-only dependency check completed. No packages installed.'
    return
}
if (-not $tar) {
    throw 'tar.exe is required (provided by supported Windows 10/11 builds). No changes were made.'
}

if (-not $node -or -not $python) {
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw 'WinGet is required for automatic bootstrap. Install Microsoft App Installer first.'
    }
    $common = @('--exact','--source','winget',
                '--accept-package-agreements','--accept-source-agreements','--silent')
    if (-not $node) {
        Write-Host 'Installing current Node.js LTS (Node + npm + npx) via WinGet...'
        & $winget.Source install --id OpenJS.NodeJS.LTS @common
        if ($LASTEXITCODE -ne 0) {
            Write-Warning 'WinGet returned a nonzero code for Node.js; checking available versions.'
        }
        Refresh-ProcessPath
        $node = Find-NodeNpx
        if (-not $node) {
            throw 'Node.js LTS / npx installation incomplete. Open a fresh PowerShell and retry.'
        }
    }
    if (-not $python) {
        Write-Host 'Installing Python 3.12 via WinGet...'
        & $winget.Source install --id Python.Python.3.12 @common
        if ($LASTEXITCODE -ne 0) {
            Write-Warning 'WinGet returned a nonzero code for Python; checking available versions.'
        }
        Refresh-ProcessPath
        $python = Find-Python
        if (-not $python) {
            throw 'Python 3.10+ installation incomplete. Open a fresh PowerShell and retry.'
        }
    }
}

# The existing CLI invokes python.exe by name. Put the confirmed executable
# first only for THIS bootstrap process, avoiding global PATH modifications.
$pythonDirectory = Split-Path -Parent $python
$nodeDirectory = Split-Path -Parent $node.Node
$env:Path = $pythonDirectory + ';' + $nodeDirectory + ';' + $env:Path

Write-Host 'Dependencies ready: Node.js, npx, Python and tar.'
if ($DependenciesOnly) {
    Write-Host ('Run: npx --yes ' + $HubOrigin + '/cli.tgz')
    return
}

Write-Host ('Starting ASC Remote Hub installer from ' + $HubOrigin)
& $node.Npx --yes ($HubOrigin + '/cli.tgz')
if ($LASTEXITCODE -ne 0) {
    throw ('ASC Remote Hub installer exited with code ' + $LASTEXITCODE)
}
