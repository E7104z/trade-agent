# =============================================================================
# SHIP TO PRODUCTION - only what was rehearsed (specs/VERIFICATION_PRACTICE_v1.md)
#
# Every evening review found errors because the first real test of a change
# was the next live session. This script refuses to deploy runtime code
# (agent/, api/, prompts/, config.json, vercel.json, requirements.txt) that no
# passing rehearsal covers:
#
#   .venv\Scripts\python run.py rehearse      # real data, scratch ledger, ~$2-3
#   powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1
#
# Steps: rehearsal check -> full test suite (never against production) ->
# git push -> vercel --prod (retried) -> one intraday tick to load any new
# schema before the next cron. -SkipWarm skips the last step.
# =============================================================================
param([switch]$SkipWarm)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

# This PC's user-level DATABASE_URL points at production. Nothing below may
# reach the production database directly; remove it for this process only.
Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue

$py = ".venv\Scripts\python.exe"

Write-Host "== 1/5 rehearsal" -ForegroundColor Cyan
& $py run.py rehearse --check
if ($LASTEXITCODE -ne 0) { throw "Not deployed: no passing rehearsal covers this commit." }

Write-Host "== 2/5 test suite" -ForegroundColor Cyan
& $py -m pytest -q -p no:cacheprovider
if ($LASTEXITCODE -ne 0) { throw "Not deployed: the test suite failed." }

Write-Host "== 3/5 git push" -ForegroundColor Cyan
git push origin master
if ($LASTEXITCODE -ne 0) { throw "Not deployed: git push failed." }

Write-Host "== 4/5 vercel --prod" -ForegroundColor Cyan
# The CLI has returned a transient "Not authorized" / "fetch failed" before;
# a retry has always gone through.
$ok = $false
foreach ($attempt in 1..3) {
    vercel --prod --yes
    if ($LASTEXITCODE -eq 0) { $ok = $true; break }
    Write-Host "vercel attempt $attempt failed; retrying" -ForegroundColor Yellow
    Start-Sleep -Seconds 10
}
if (-not $ok) { throw "vercel --prod failed three times." }

if (-not $SkipWarm) {
    Write-Host "== 5/5 warm the schema (one intraday tick)" -ForegroundColor Cyan
    powershell -ExecutionPolicy Bypass -File scripts\intraday_cron.ps1 -Once
}
Write-Host "Deployed." -ForegroundColor Green
