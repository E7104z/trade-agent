# Registers the three Trade Agent loops as Windows Scheduled Tasks.
# Run from an ELEVATED PowerShell:  .\scripts\setup_tasks.ps1
#
# NOTE: Times below are LOCAL machine time. If your machine is not in US Eastern,
# adjust to match 6:45 AM / 4:30 PM ET. The loops themselves reason in ET internally.

$projectDir = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectDir ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }

function Register-LoopTask {
    param([string]$Name, [string]$Loop, [string]$Time, [string[]]$Days)
    $action = New-ScheduledTaskAction -Execute $python -Argument "run.py $Loop" -WorkingDirectory $projectDir
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $Days -At $Time
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger `
        -Settings $settings -Description "Trade Agent $Loop loop" -Force | Out-Null
    Write-Host "Registered: $Name ($Loop at $Time on $($Days -join ','))"
}

$weekdays = @("Monday","Tuesday","Wednesday","Thursday","Friday")
Register-LoopTask -Name "TradeAgent-Morning" -Loop "morning" -Time "6:45AM"  -Days $weekdays
Register-LoopTask -Name "TradeAgent-Evening" -Loop "evening" -Time "4:30PM"  -Days $weekdays
Register-LoopTask -Name "TradeAgent-Weekly"  -Loop "weekly"  -Time "5:00PM"  -Days @("Sunday")

Write-Host ""
Write-Host "Done. Machine must be on (or waking) at trigger times."
Write-Host "Remove with: Unregister-ScheduledTask -TaskName TradeAgent-Morning,TradeAgent-Evening,TradeAgent-Weekly"
