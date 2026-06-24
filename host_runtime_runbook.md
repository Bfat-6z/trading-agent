# Host Runtime Runbook

Purpose: keep the paper-learning agent available 24/7 without enabling live orders.

## Windows Autostart

Use Task Scheduler to run the supervisor at login:

```powershell
$Root = "E:\keo-moi-mail\trading-agent"
$Python = Join-Path $Root "venv\Scripts\python.exe"
$Action = New-ScheduledTaskAction -Execute $Python -Argument "agent_process_supervisor.py" -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -AtLogOn
Register-ScheduledTask -TaskName "TradingAgentPaperSupervisor" -Action $Action -Trigger $Trigger -Description "Paper-only trading agent supervisor" -User $env:USERNAME
```

Set `TRADING_AGENT_AUTOSTART_CONFIRMED=1` only after the task starts successfully.

## Power Settings

- Disable sleep while paper learning is expected to run.
- Keep network adapter power saving disabled.
- Keep dashboard readable; use kill switch instead of killing state files.

## Safe Startup Check

```powershell
venv\Scripts\python.exe agent_process_supervisor.py --status
venv\Scripts\python.exe test_harness.py --run-tests tests -q
```

Live orders remain disabled by `runtime_config.py` and `live_permission_firewall.py`.
