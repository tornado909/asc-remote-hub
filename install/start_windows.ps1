$ErrorActionPreference='Stop'
$config=Join-Path $env:APPDATA 'ASC-Remote-Hub\agent.json'
if(-not (Test-Path -LiteralPath $config)){throw "Not enrolled: $config"}
$venvPython=Join-Path $PSScriptRoot '..\.venv\Scripts\python.exe'
$python=if(Test-Path -LiteralPath $venvPython){(Resolve-Path $venvPython).Path}else{(Get-Command python.exe -ErrorAction Stop).Source}
$worker=(Resolve-Path (Join-Path $PSScriptRoot '..\client\bridge.py')).Path
& $python $worker run --config $config
