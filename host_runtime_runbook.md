# Host Runtime Runbook

Purpose: keep the paper-learning agent available 24/7 without enabling live orders.

## Windows Autostart

Use Task Scheduler to run the hidden, noninteractive supervisor runner at startup. The runner uses `Set-StrictMode`, `$ErrorActionPreference = "Stop"`, `Set-Location -LiteralPath`, sanitized `PATH/PATHEXT/PYTHONPATH`, `PYTHONUTF8=1`, absolute venv Python path, and propagates `$LASTEXITCODE`.

```powershell
$Root = "E:\keo-moi-mail\trading-agent"
$Runner = Join-Path $Root "scripts\run_supervisor_hidden.ps1"
$PowerShell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$Action = New-ScheduledTaskAction -Execute $PowerShell -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Runner`"" -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -AtStartup
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Highest
Register-ScheduledTask -TaskName "TradingAgentPaperSupervisor" -Action $Action -Trigger $Trigger -Principal $Principal -Description "Paper-only trading agent supervisor"
Start-ScheduledTask -TaskName "TradingAgentPaperSupervisor"
Get-ScheduledTaskInfo -TaskName "TradingAgentPaperSupervisor"
```

After the task starts successfully, write `state/autostart_proof.json` instead of relying on an env flag:

```json
{
  "trigger": "AtStartup",
  "working_dir": "E:\\keo-moi-mail\\trading-agent",
  "venv_python": "E:\\keo-moi-mail\\trading-agent\\venv\\Scripts\\pythonw.exe",
  "user_context": "ACER",
  "env_source": "sanitized_env",
  "run_whether_user_logged_on": true,
  "post_reboot_assertion": true,
  "verification_source": "task_scheduler",
  "task_query_ok": true,
  "verified_at": "2026-06-30T00:00:00+07:00"
}
```

`host_runtime_monitor.py` rejects false-string values such as `"false"` and requires `verification_source`, `task_query_ok`, and `verified_at`.

## Power Settings

- Disable sleep while paper learning is expected to run.
- Keep network adapter power saving disabled.
- Keep dashboard readable; use kill switch instead of killing state files.

## Safe Startup Check

```powershell
Set-Location -LiteralPath "E:\keo-moi-mail\trading-agent"
.\venv\Scripts\python.exe agent_process_supervisor.py --status
.\venv\Scripts\python.exe -m pytest tests\test_phase_21_reliability_recovery.py -q
Get-ScheduledTaskInfo -TaskName "TradingAgentPaperSupervisor"
```

Expected output:

- `agent_process_supervisor.py --status` prints JSON with supervised agents.
- pytest exits `0`.
- `Get-ScheduledTaskInfo` shows a recent `LastRunTime` and `LastTaskResult` equal to `0`.

Rollback:

```powershell
Unregister-ScheduledTask -TaskName "TradingAgentPaperSupervisor" -Confirm:$false
```

Live orders remain disabled by `runtime_config.py` and `live_permission_firewall.py`.
