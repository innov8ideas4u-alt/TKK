# burn_audit.ps1 - TKK Phase 1 burn audit
# Reads ~/.tkk/decisions.jsonl and counter shards, reports savings.
# Usage: powershell -NoProfile -File burn_audit.ps1 [-TkkHome <path>] [-EstTokensPerBlock 5000]

[CmdletBinding()]
param(
    [string]$TkkHome = (Join-Path $env:USERPROFILE ".tkk"),
    [int]$EstTokensPerBlock = 5000,
    [int]$ArchiveDays = 30
)

$ErrorActionPreference = "Stop"

$decisionsPath = Join-Path $TkkHome "decisions.jsonl"
$countersDir = Join-Path $TkkHome "counters"
$archiveDir = Join-Path $countersDir "_archive"

if (-not (Test-Path $TkkHome)) {
    Write-Host "TKK home not found: $TkkHome" -ForegroundColor Yellow
    exit 1
}

# Archive counter shards older than $ArchiveDays
if (Test-Path $countersDir) {
    New-Item -ItemType Directory -Force -Path $archiveDir | Out-Null
    $cutoff = (Get-Date).AddDays(-$ArchiveDays)
    Get-ChildItem -Path $countersDir -Filter "*.json" -File | Where-Object {
        $_.LastWriteTime -lt $cutoff
    } | ForEach-Object {
        Move-Item $_.FullName (Join-Path $archiveDir $_.Name) -Force
    }
}

# Aggregate counters (across all session shards, including archive? -> live only)
$agg = @{
    total_invocations = 0
    allowed_rule_a = 0
    allowed_rule_b = 0
    allowed_rule_c_overlap_ok = 0
    allowed_bypass_override = 0
    allowed_bypass_fresh_path = 0
    allowed_bypass_fresh_age = 0
    allowed_bypass_file_changed = 0
    blocked_rule_c = 0
    blocked_rule_d = 0
    lock_timeouts = 0
    errors = 0
}

if (Test-Path $countersDir) {
    Get-ChildItem -Path $countersDir -Filter "*.json" -File | ForEach-Object {
        try {
            $obj = Get-Content $_.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
            foreach ($k in @($agg.Keys)) {
                if ($obj.PSObject.Properties.Name -contains $k) {
                    $agg[$k] = [int]$agg[$k] + [int]$obj.$k
                }
            }
        } catch {
            Write-Warning "Skipping bad shard: $($_.FullName) - $($_.Exception.Message)"
        }
    }
}

# Top blocked / overridden files from decisions.jsonl
$blockedFiles = @{}
$overrideFiles = @{}
$decisionsCount = 0

if (Test-Path $decisionsPath) {
    Get-Content $decisionsPath -Encoding UTF8 | ForEach-Object {
        $line = $_.Trim()
        if (-not $line) { return }
        try {
            $obj = $line | ConvertFrom-Json
        } catch { return }
        $decisionsCount++
        $decision = [string]$obj.decision
        $file = [string]$obj.file_orig
        if (-not $file) { $file = [string]$obj.file }
        if ($decision -eq "block") {
            if ($blockedFiles.ContainsKey($file)) { $blockedFiles[$file]++ } else { $blockedFiles[$file] = 1 }
        } elseif ($decision -eq "override") {
            if ($overrideFiles.ContainsKey($file)) { $overrideFiles[$file]++ } else { $overrideFiles[$file] = 1 }
        }
    }
}

$totalBlocks = [int]$agg["blocked_rule_c"] + [int]$agg["blocked_rule_d"]
$tokensSaved = $totalBlocks * $EstTokensPerBlock

Write-Host ""
Write-Host "=== TKK Burn Audit ===" -ForegroundColor Cyan
Write-Host ("TKK home:       {0}" -f $TkkHome)
Write-Host ("decisions.jsonl: {0} entries" -f $decisionsCount)
Write-Host ""
Write-Host "--- Aggregate counters (all live shards) ---"
$agg.GetEnumerator() | Sort-Object Name | ForEach-Object {
    Write-Host ("  {0,-32} {1,8}" -f $_.Key, $_.Value)
}
Write-Host ""
Write-Host ("Total blocks:        {0}" -f $totalBlocks)
Write-Host ("Estimated tokens saved: {0:N0}  (at {1} tok/block)" -f $tokensSaved, $EstTokensPerBlock)
Write-Host ""

Write-Host "--- Top 10 blocked files ---"
$blockedFiles.GetEnumerator() | Sort-Object Value -Descending | Select-Object -First 10 | ForEach-Object {
    Write-Host ("  {0,5}  {1}" -f $_.Value, $_.Key)
}
Write-Host ""
Write-Host "--- Top 10 overridden files ---"
$overrideFiles.GetEnumerator() | Sort-Object Value -Descending | Select-Object -First 10 | ForEach-Object {
    Write-Host ("  {0,5}  {1}" -f $_.Value, $_.Key)
}

# Write JSON summary
$stamp = (Get-Date -Format "yyyyMMdd_HHmmss")
$reportPath = Join-Path $TkkHome "burn_report_$stamp.json"
$summary = [ordered]@{
    generated_at = (Get-Date).ToString("o")
    tkk_home = $TkkHome
    decisions_count = $decisionsCount
    counters = $agg
    total_blocks = $totalBlocks
    estimated_tokens_saved = $tokensSaved
    est_tokens_per_block = $EstTokensPerBlock
    top_blocked_files = ($blockedFiles.GetEnumerator() | Sort-Object Value -Descending | Select-Object -First 10 | ForEach-Object { [ordered]@{ file = $_.Key; count = $_.Value } })
    top_override_files = ($overrideFiles.GetEnumerator() | Sort-Object Value -Descending | Select-Object -First 10 | ForEach-Object { [ordered]@{ file = $_.Key; count = $_.Value } })
}
$summary | ConvertTo-Json -Depth 6 | Set-Content -Path $reportPath -Encoding UTF8
Write-Host ""
Write-Host ("JSON summary: {0}" -f $reportPath) -ForegroundColor Green
