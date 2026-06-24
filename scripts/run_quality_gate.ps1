$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root "venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
  $Python = "python"
}
& $Python (Join-Path $Root "test_harness.py") --run-tests tests -q
