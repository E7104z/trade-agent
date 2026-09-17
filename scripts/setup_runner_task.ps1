# Registers the intraday Runner lane as a Windows Scheduled Task.
#
# Run ONCE from an ELEVATED PowerShell:
#     .\scripts\setup_runner_task.ps1
#
# WHY THIS EXISTS
# The hosted path for this lane is POST /cron/intraday-check driven by a
# scheduler. Neither scheduler available to this project can do it:
#   - GitHub Actions returns startup_failure for every run on this account
#     (verified account-level: a trivial `echo hello` workflow fails the same
#     way), so the workflow has never executed once.
#   - Vercel Cron on the Hobby plan rejects any schedule firing more than once
#     a day, and refuses the deploy outright.
# So the scan runs from this machine instead. Orders go to the SAME Alpaca
# paper account either way; the only difference is which database records the
# evidence.
#
# The task starts once a day and the process loops internally until the scan
# window closes, so Task Scheduler only has to fire it once.

param(
    # Paste your Postgres connection string to send results to the web
    # dashboard. Stored as a USER environment variable on this machine only;
    # it is never written to the repo, never logged, and never leaves here.
    [string]$DatabaseUrl
)

$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectDir ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }

# --- Local time for the 10:15 ET start ------------------------------------
# The loop re-checks the window itself on every tick, so starting a few minutes
# early is harmless: pre-window ticks return "before scan_start_et" and cost
# nothing. Starting LATE would silently skip the 10:30 decision, so err early.
$etNow    = [System.TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTime]::UtcNow, "Eastern Standard Time")
$localNow = Get-Date
$offsetHours = [Math]::Round(($localNow - $etNow).TotalHours)
$startLocal  = (Get-Date -Hour 10 -Minute 15 -Second 0).AddHours($offsetHours)
$startStr    = $startLocal.ToString("HH:mm")

Write-Host "This machine is $offsetHours h from ET; 10:15 ET = $startStr local."

# --- Where does the evidence land? ----------------------------------------
# agent/ledger.py connects to Postgres when DATABASE_URL is set, and to the
# local DuckDB file otherwise. The hosted dashboard at iwilltrade.com reads
# Postgres. So without DATABASE_URL the scan still runs and still places the
# same paper orders, but the dashboard shows nothing and the lane looks dead.
if ($DatabaseUrl) {
    if ($DatabaseUrl -notmatch '^postgres(ql)?://') {
        throw "That does not look like a Postgres URL (expected it to start with postgres:// or postgresql://)."
    }
    [Environment]::SetEnvironmentVariable("DATABASE_URL", $DatabaseUrl, "User")
    $env:DATABASE_URL = $DatabaseUrl
    Write-Host "DATABASE_URL saved for your Windows user account." -ForegroundColor Green
}

$dbUrl = [Environment]::GetEnvironmentVariable("DATABASE_URL", "User")
if (-not $dbUrl) { $dbUrl = $env:DATABASE_URL }
if ($dbUrl) {
    Write-Host "DATABASE_URL is set - results will appear on the web dashboard."
} else {
    Write-Host ""
    Write-Host "NOTE: DATABASE_URL is not set." -ForegroundColor Yellow
    Write-Host "  The scan will run and place orders, but its results go to the"
    Write-Host "  LOCAL database, so iwilltrade.com will keep showing 'no scan"
    Write-Host "  recorded'."
    Write-Host ""
    Write-Host "  Vercel cannot show you the value again - DATABASE_URL is marked"
    Write-Host "  Sensitive, so the box you see there is an empty placeholder."
    Write-Host "  Get the connection string from the database itself (Neon:"
    Write-Host "  console.neon.tech > your project > Connection string), then:"
    Write-Host ""
    Write-Host "    .\scripts\setup_runner_task.ps1 -DatabaseUrl '<paste it here>'" -ForegroundColor Cyan
    Write-Host ""
}

$taskName = "TradeAgent-Runner"
$action = New-ScheduledTaskAction -Execute $python `
    -Argument "run.py runner --loop --every 300" `
    -WorkingDirectory $projectDir
$trigger = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At $startStr
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun `
    -ExecutionTimeLimit (New-TimeSpan -Hours 6) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "DTO Runner intraday scan, select and place" `
    -Force | Out-Null

Write-Host ""
Write-Host "Registered: $taskName  ($startStr local, Mon-Fri)"
Write-Host ""
Write-Host "It will scan every 5 minutes from 10:20 ET, pick 10 at 10:30 ET,"
Write-Host "place paper orders, then keep scanning and recording until 15:00 ET."
Write-Host ""
Write-Host "Verify now without waiting:"
Write-Host "    .venv\Scripts\python.exe run.py runner --no-orders"
Write-Host ""
Write-Host "Run it immediately:"
Write-Host "    Start-ScheduledTask -TaskName $taskName"
Write-Host ""
Write-Host "Watch what it found:"
Write-Host "    .venv\Scripts\python.exe run.py status"
Write-Host ""
Write-Host "Stop it:"
Write-Host "    Unregister-ScheduledTask -TaskName $taskName -Confirm:`$false"
