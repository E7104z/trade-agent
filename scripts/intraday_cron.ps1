# =============================================================================
# LOCAL INTRADAY TRIGGER - stands in for the dead GitHub Actions cron.
#
# Why this exists
# ---------------
# /cron/intraday-check is what runs BOTH the live paper position monitor and
# the runner lane. Nothing is currently calling it:
#
#   * GitHub Actions  - every scheduled run since at least 2026-09-09 ends as
#                       startup_failure in 0s with zero jobs and zero billable
#                       time. A trivial "echo hello" workflow fails the same
#                       way, so it is an account-level block (Actions minutes
#                       or a spending limit on a private repo), not the YAML.
#   * Vercel Cron     - rejected at deploy time: "Hobby accounts are limited to
#                       daily cron jobs." Needs Pro for a */5 schedule.
#
# This script polls the production endpoint from this machine instead. It is a
# stopgap: it only works while the machine is awake and online.
#
# Usage
# -----
#   $env:TRADE_AGENT_CRON_SECRET = "<the CRON_SECRET from Vercel>"
#   powershell -ExecutionPolicy Bypass -File scripts\intraday_cron.ps1
#
# Get the secret from:
#   Vercel -> trade-agent -> Settings -> Environment Variables -> CRON_SECRET
#
# Optional:
#   -BaseUrl        default https://iwilltrade.com
#   -IntervalSec    default 300 (5 minutes)
#   -StartEt/-EndEt default 09:40 / 15:59 ET. The end runs to the close, not
#                   just the 15:00 scan window: the live monitor flattens any
#                   open paper position at 15:55 ET (bracket legs are day
#                   orders and expire at the close, which would otherwise
#                   leave the position open overnight)
#   -Once           fire a single request and exit (use this to test the secret)
#
# The server re-checks market day, holiday and the scan window on every call,
# so an extra tick outside the window is a cheap no-op, never a bad trade.
# =============================================================================

param(
    [string]$BaseUrl     = "https://iwilltrade.com",
    [int]   $IntervalSec = 300,
    [string]$StartEt     = "09:40",
    [string]$EndEt       = "15:59",
    [switch]$Once
)

$ErrorActionPreference = "Stop"

$secret = $env:TRADE_AGENT_CRON_SECRET
if (-not $secret) {
    Write-Host "ERROR: TRADE_AGENT_CRON_SECRET is not set." -ForegroundColor Red
    Write-Host ""
    Write-Host "  `$env:TRADE_AGENT_CRON_SECRET = '<CRON_SECRET from Vercel>'"
    Write-Host "  powershell -ExecutionPolicy Bypass -File scripts\intraday_cron.ps1"
    Write-Host ""
    Write-Host "Vercel -> trade-agent -> Settings -> Environment Variables -> CRON_SECRET"
    exit 1
}

$url = "$($BaseUrl.TrimEnd('/'))/cron/intraday-check"
$headers = @{ Authorization = "Bearer $secret" }

function Get-EtNow {
    # This machine is Central; convert explicitly rather than assuming an offset
    # so the window still holds if the machine's timezone ever changes.
    $tz = [System.TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time")
    return [System.TimeZoneInfo]::ConvertTime([DateTime]::UtcNow, [System.TimeZoneInfo]::Utc, $tz)
}

function Invoke-Tick {
    $et = Get-EtNow
    try {
        $resp = Invoke-RestMethod -Uri $url -Method Post -Headers $headers -TimeoutSec 300
        $json = $resp | ConvertTo-Json -Depth 6 -Compress
        if ($json.Length -gt 600) { $json = $json.Substring(0, 600) + "..." }
        Write-Host "[$($et.ToString('HH:mm:ss')) ET] 200  $json" -ForegroundColor Green
    }
    catch {
        # 401 means the secret is wrong; anything else is usually transient.
        $code = $null
        if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
        $msg = $_.Exception.Message
        if ($code -eq 401) {
            Write-Host "[$($et.ToString('HH:mm:ss')) ET] 401 UNAUTHORIZED - CRON_SECRET is wrong." -ForegroundColor Red
        } else {
            Write-Host "[$($et.ToString('HH:mm:ss')) ET] FAILED ($code) $msg" -ForegroundColor Yellow
        }
    }
}

if ($Once) {
    Write-Host "Single tick against $url"
    Invoke-Tick
    exit 0
}

$start = [DateTime]::ParseExact($StartEt, "HH:mm", $null).TimeOfDay
$end   = [DateTime]::ParseExact($EndEt,   "HH:mm", $null).TimeOfDay

Write-Host "Intraday trigger -> $url"
Write-Host "Window $StartEt-$EndEt ET, every $IntervalSec s. Ctrl+C to stop."
Write-Host ""

while ($true) {
    $et = Get-EtNow
    $t  = $et.TimeOfDay

    if ($et.DayOfWeek -eq 'Saturday' -or $et.DayOfWeek -eq 'Sunday') {
        Write-Host "[$($et.ToString('ddd HH:mm')) ET] weekend - sleeping 1h" -ForegroundColor DarkGray
        Start-Sleep -Seconds 3600
        continue
    }
    if ($t -lt $start) {
        $wait = [int]([math]::Min(($start - $t).TotalSeconds, 600))
        Write-Host "[$($et.ToString('HH:mm:ss')) ET] before window - sleeping $wait s" -ForegroundColor DarkGray
        Start-Sleep -Seconds ([math]::Max($wait, 15))
        continue
    }
    if ($t -gt $end) {
        Write-Host "[$($et.ToString('HH:mm:ss')) ET] past window - done for today." -ForegroundColor Cyan
        Start-Sleep -Seconds 3600
        continue
    }

    Invoke-Tick
    Start-Sleep -Seconds $IntervalSec
}
