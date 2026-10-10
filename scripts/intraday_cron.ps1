# =============================================================================
# LOCAL INTRADAY TRIGGER - drives /cron/intraday-check every 5 minutes.
#
# Why this exists
# ---------------
# /cron/intraday-check runs the live paper position monitor (fills, time
# stops, the 15:55 ET end-of-day flatten, overnight-carry cleanup), server-side
# against the production ledger. Nothing on the hosted side can call it often enough:
#
#   * Vercel Cron     - Hobby plan crons run at most once per day.
#   * GitHub Actions  - every scheduled run since 2026-09-09 ends as
#                       startup_failure (account-level block on the private
#                       repo), and its */5 schedule is best-effort anyway.
#
# So this machine polls the production endpoint. It only works while the
# machine is awake and online; the dashboard's "Intraday monitor" stage shows
# when it last ran. (Vercel Pro would make this unnecessary: */5 crons.)
#
# Secret
# ------
# Uses INTRADAY_TRIGGER_SECRET: it authorizes /cron/intraday-check and NOTHING
# else (unlike CRON_SECRET, which passes every protected endpoint). Read from,
# in order:
#   1. $env:TRADE_AGENT_CRON_SECRET
#   2. the file  %USERPROFILE%\.trade-agent\intraday_trigger_secret
#
# Usage
# -----
#   powershell -ExecutionPolicy Bypass -File scripts\intraday_cron.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\intraday_cron.ps1 -Once
#
# Optional:
#   -BaseUrl        default https://iwilltrade.com
#   -IntervalSec    default 300 (5 minutes). Ticks are aligned to the wall
#                   clock (09:35, 09:40, ... 15:55), so the 15:55 flatten
#                   always gets a tick no matter how long earlier ticks took.
#   -StartEt/-EndEt default 09:35 / 15:59 ET
#   -LogFile        default %USERPROFILE%\.trade-agent\intraday.log
#   -Once           fire a single request and exit (use this to test)
#
# The server re-checks market day, holiday and every window on each call, so
# an extra tick outside the window is a cheap no-op, never a bad trade.
# =============================================================================

param(
    [string]$BaseUrl     = "https://iwilltrade.com",
    [int]   $IntervalSec = 300,
    [string]$StartEt     = "09:35",
    [string]$EndEt       = "15:59",
    [string]$LogFile     = "",
    [switch]$Once
)

$ErrorActionPreference = "Stop"

$stateDir = Join-Path $env:USERPROFILE ".trade-agent"
if (-not $LogFile) { $LogFile = Join-Path $stateDir "intraday.log" }

function Write-Log([string]$line, [string]$color = "Gray") {
    Write-Host $line -ForegroundColor $color
    try {
        if (-not (Test-Path $stateDir)) { New-Item -ItemType Directory -Force $stateDir | Out-Null }
        if ((Test-Path $LogFile) -and ((Get-Item $LogFile).Length -gt 5MB)) {
            Move-Item -Force $LogFile "$LogFile.old"
        }
        Add-Content -Path $LogFile -Value $line -Encoding ASCII
    } catch { }
}

$secret = $env:TRADE_AGENT_CRON_SECRET
if (-not $secret) {
    $secretFile = Join-Path $stateDir "intraday_trigger_secret"
    if (Test-Path $secretFile) { $secret = (Get-Content $secretFile -Raw).Trim() }
}
if (-not $secret) {
    Write-Log "ERROR: no trigger secret. Set `$env:TRADE_AGENT_CRON_SECRET or write it to $stateDir\intraday_trigger_secret" "Red"
    exit 1
}

$url = "$($BaseUrl.TrimEnd('/'))/cron/intraday-check"
$headers = @{ Authorization = "Bearer $secret" }

function Get-EtNow {
    # Convert explicitly rather than assuming this machine's offset, so the
    # window still holds if the machine's timezone ever changes.
    $tz = [System.TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time")
    return [System.TimeZoneInfo]::ConvertTime([DateTime]::UtcNow, [System.TimeZoneInfo]::Utc, $tz)
}

function Invoke-Tick {
    $et = Get-EtNow
    try {
        $resp = Invoke-RestMethod -Uri $url -Method Post -Headers $headers -TimeoutSec 300
        $json = $resp | ConvertTo-Json -Depth 6 -Compress
        if ($json.Length -gt 600) { $json = $json.Substring(0, 600) + "..." }
        Write-Log "[$($et.ToString('yyyy-MM-dd HH:mm:ss')) ET] 200  $json" "Green"
    }
    catch {
        # 401 means the secret is wrong; anything else is usually transient.
        $code = $null
        if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
        $msg = $_.Exception.Message
        if ($code -eq 401) {
            Write-Log "[$($et.ToString('yyyy-MM-dd HH:mm:ss')) ET] 401 UNAUTHORIZED - the trigger secret is wrong." "Red"
        } else {
            Write-Log "[$($et.ToString('yyyy-MM-dd HH:mm:ss')) ET] FAILED ($code) $msg" "Yellow"
        }
    }
}

if ($Once) {
    Write-Log "Single tick against $url"
    Invoke-Tick
    exit 0
}

$start = [DateTime]::ParseExact($StartEt, "HH:mm", $null).TimeOfDay
$end   = [DateTime]::ParseExact($EndEt,   "HH:mm", $null).TimeOfDay

Write-Log "Intraday trigger -> $url  (window $StartEt-$EndEt ET, every $IntervalSec s, wall-clock aligned)"

while ($true) {
    $et = Get-EtNow
    $t  = $et.TimeOfDay

    if ($et.DayOfWeek -eq 'Saturday' -or $et.DayOfWeek -eq 'Sunday') {
        Write-Log "[$($et.ToString('ddd HH:mm')) ET] weekend - done." "DarkGray"
        exit 0
    }
    if ($t -gt $end) {
        Write-Log "[$($et.ToString('HH:mm:ss')) ET] past window - done for today." "Cyan"
        exit 0
    }
    if ($t -ge $start) {
        Invoke-Tick
    }

    # Sleep to the next wall-clock boundary (+2s), never a fixed interval after
    # the tick: a fixed sleep drifts by each tick's own duration (~30-60s), and
    # over a session that drift can skip the 15:55 flatten entirely.
    $et = Get-EtNow
    $secs = $et.TimeOfDay.TotalSeconds
    $next = [math]::Floor($secs / $IntervalSec) * $IntervalSec + $IntervalSec + 2
    if ($secs -lt $start.TotalSeconds) { $next = [math]::Max($next, $start.TotalSeconds + 2) }
    Start-Sleep -Seconds ([int][math]::Max(5, $next - $secs))
}
