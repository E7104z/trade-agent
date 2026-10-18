# =============================================================================
# SHIP THE 2026-10 FIXES -- run once (docs/FIX_VERIFICATION.md)
#
#   powershell -ExecutionPolicy Bypass -File scripts\ship_fixes.ps1           # do it
#   powershell -ExecutionPolicy Bypass -File scripts\ship_fixes.ps1 -DryRun   # show the steps only
#
# Jason decided on 2026-10-17: the follow-up patch is applied (already in the
# tree), shadow history is re-priced in full, taken picks are re-graded under
# simulator v8 from 2026-10-02, entry-semantics enforcement stays off.
#
#   1. test suite (never against production)
#   2. commit the working tree, if anything is uncommitted
#   3. rehearse, unless a passing rehearsal already covers this commit (~$2-3)
#   4. deploy: scripts\deploy.ps1 (suite, git push, vercel --prod, schema warm-up)
#   5. impact report, read-only, saved to reports\fix_impact_<date>.txt
#   6. shadow history re-priced  (run.py regrade-shadow --from 2026-01-01 --apply --force)
#   7. taken picks re-graded     (run.py regrade --from 2026-10-02 --force)
#
# Any failure stops it there; nothing after a failed step runs, and running it
# again picks up safely (the rehearsal is not paid for twice). Once it has
# finished there is nothing left to remember: the evening email's SELF-CHECK
# verifies the fixes every session ("Fix verification (5 sessions)", VERIFIED
# after 5 straight passes) and says DECISION DUE when entry-semantics
# enforcement has its evidence ("Entry semantics (D5)").
# =============================================================================
param([switch]$DryRun, [switch]$Again)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$py = ".venv\Scripts\python.exe"
$today = (Get-Date).ToString("yyyy-MM-dd")
$report = "reports\fix_impact_$today.txt"
$marker = Join-Path $env:USERPROFILE ".trade-agent\fixes_shipped.txt"
$commitMessage = "Fix-it session: broker-truth flatten, sim v8, broker headline, shadow ledger, entry semantics, order mechanics, ETF prices, self-verifying fixes"
$coAuthor = "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"

if ((Test-Path $marker) -and -not $Again) {
    Write-Host "Already shipped:" -ForegroundColor Green
    Get-Content $marker | ForEach-Object { Write-Host "  $_" }
    Write-Host "Nothing to do. (-Again redoes every step.)"
    exit 0
}

# Production is only touched in steps 5-7, and only through this variable.
$prodDsn = [Environment]::GetEnvironmentVariable("DATABASE_URL", "User")
if (-not $prodDsn) { throw "The user-level DATABASE_URL (production) is not set; steps 5-7 need it." }
Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue

function Invoke-Step([string]$title, [scriptblock]$action) {
    Write-Host ""
    Write-Host "== $title" -ForegroundColor Cyan
    if ($DryRun) {
        Write-Host ("   would run: " + ($action.ToString().Trim() -replace "\s+", " "))
        return
    }
    $global:LASTEXITCODE = 0
    & $action
    if ($LASTEXITCODE -ne 0) { throw "Stopped at '$title' (exit $LASTEXITCODE). Nothing after it ran." }
}

Invoke-Step "1/7 test suite (never against production)" {
    & $py -m pytest -q -p no:cacheprovider
}

Invoke-Step "2/7 commit the working tree" {
    if (git status --porcelain) {
        git add -A
        git commit -q -m $commitMessage -m $coAuthor
    } else {
        Write-Host "   nothing to commit"
    }
}

Invoke-Step "3/7 rehearse (skipped when a passing rehearsal covers this commit)" {
    & $py run.py rehearse --check
    if ($LASTEXITCODE -eq 2) { throw "A cron is due: run this again in an hour (the deploy gate refuses now)." }
    if ($LASTEXITCODE -ne 0) { & $py run.py rehearse }
}

Invoke-Step "4/7 deploy (suite, git push, vercel --prod, schema warm-up)" {
    powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1
}

# stdout only: in Windows PowerShell 5.1 a redirected stderr line under ErrorActionPreference
# Stop becomes a terminating error, so warnings stay on the console and out of the report.
Invoke-Step "5/7 impact report, read-only -> $report" {
    $env:DATABASE_URL = $prodDsn
    New-Item -ItemType Directory -Force reports | Out-Null
    & $py scripts\verify_fixes.py --impact | Tee-Object -FilePath $report
}

Invoke-Step "6/7 re-price the shadow ledger's whole history" {
    $env:DATABASE_URL = $prodDsn
    & $py run.py regrade-shadow --from 2026-01-01 --apply --force
}

Invoke-Step "7/7 re-grade taken picks under simulator v8 from 2026-10-02" {
    $env:DATABASE_URL = $prodDsn
    & $py run.py regrade --from 2026-10-02 --force
}

Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
if ($DryRun) { Write-Host ""; Write-Host "Dry run: nothing was done." -ForegroundColor Yellow; exit 0 }

New-Item -ItemType Directory -Force (Split-Path $marker) | Out-Null
@("shipped $((Get-Date).ToString('yyyy-MM-dd HH:mm')) from commit $(git rev-parse --short HEAD)",
  "impact report: $report",
  "shadow history re-priced from 2026-01-01; taken picks re-graded under sim v8 from 2026-10-02") |
    Set-Content -Encoding utf8 $marker

Write-Host ""
Write-Host "Shipped. Nothing left to remember:" -ForegroundColor Green
Write-Host "  - the evening email's SELF-CHECK 'Fix verification (5 sessions)' judges every session"
Write-Host "    and says VERIFIED after 5 straight passes, then retires itself;"
Write-Host "  - 'Entry semantics (D5)' says DECISION DUE when enforcement has its evidence;"
Write-Host "  - the impact report is in $report."
