Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $Root "venv\Scripts\pythonw.exe"
if (-not (Test-Path -LiteralPath $Python)) {
  $Python = Join-Path $Root "venv\Scripts\python.exe"
}
if (-not (Test-Path -LiteralPath $Python)) {
  throw "Python interpreter not found under $Root\venv\Scripts"
}

Set-Location -LiteralPath $Root
$env:PYTHONUTF8 = "1"
$env:PYTHONPATH = $Root
$env:PATH = "$Root\venv\Scripts;$env:SystemRoot\System32;$env:SystemRoot"
$env:PATHEXT = ".COM;.EXE;.BAT;.CMD;.PS1"

& $Python (Join-Path $Root "agent_process_supervisor.py")
exit $LASTEXITCODE
