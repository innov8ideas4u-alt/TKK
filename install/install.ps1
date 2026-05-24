# install.ps1 - TKK Phase 1 Read Guard installer (8-step per spec v5)
# Idempotent. Backs up settings.json before merge. Atomic .tmp + Move-Item.
# Run: powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1

[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [switch]$SkipVersionCheck
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

if ($PSVersionTable.PSVersion.Major -lt 6) {
    Write-Host "ERROR: This installer requires PowerShell 7+ (pwsh). Detected: $($PSVersionTable.PSVersion)" -ForegroundColor Red
    Write-Host "       Re-run with: pwsh -NoProfile -ExecutionPolicy Bypass -File $PSCommandPath" -ForegroundColor Yellow
    exit 1
}

if ([string]::IsNullOrEmpty($RepoRoot)) {
    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
    $RepoRoot = Split-Path -Parent $scriptDir
}

Write-Host ""
Write-Host "=== TKK Phase 1 Read Guard - Installer ===" -ForegroundColor Cyan
Write-Host ""

# ---------------------------------------------------------------------------
# STEP 1 - Pre-flight verification
# ---------------------------------------------------------------------------
Write-Host "[1/8] Pre-flight verification..." -ForegroundColor Yellow

# Python >= 3.10
try {
    $pyRaw = & python --version 2>&1 | Out-String
} catch {
    Write-Host "ERROR: python not on PATH." -ForegroundColor Red
    exit 1
}
$pyMatch = [regex]::Match($pyRaw, '(\d+)\.(\d+)\.(\d+)')
if (-not $pyMatch.Success) {
    Write-Host "ERROR: could not parse python version: $pyRaw" -ForegroundColor Red
    exit 1
}
$pyVer = [Version]::new([int]$pyMatch.Groups[1].Value, [int]$pyMatch.Groups[2].Value, [int]$pyMatch.Groups[3].Value)
if ($pyVer -lt [Version]"3.10.0") {
    Write-Host ("ERROR: python {0} < 3.10. Install Python >= 3.10." -f $pyVer) -ForegroundColor Red
    exit 1
}
Write-Host ("      python: {0}" -f $pyVer)

# claude >= 2.0.10
if (-not $SkipVersionCheck) {
    & (Join-Path $PSScriptRoot "verify_version.ps1")
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ABORT: claude version check failed." -ForegroundColor Red
        exit 1
    }
}

# Source hook file exists
$srcHook = Join-Path $RepoRoot "hooks\tkk_read_guard.py"
if (-not (Test-Path $srcHook)) {
    Write-Host "ERROR: source hook not found at $srcHook" -ForegroundColor Red
    exit 1
}
Write-Host "      source hook: $srcHook"

# ---------------------------------------------------------------------------
# STEP 2 - Create state directories
# ---------------------------------------------------------------------------
Write-Host "[2/8] Creating state directories..." -ForegroundColor Yellow
$tkkHome = Join-Path $env:USERPROFILE ".tkk"
$claudeHooks = Join-Path $env:USERPROFILE ".claude\hooks"
foreach ($d in @($tkkHome, (Join-Path $tkkHome "read_log"), (Join-Path $tkkHome "counters"), (Join-Path $tkkHome "locks"), $claudeHooks)) {
    New-Item -ItemType Directory -Force -Path $d | Out-Null
    Write-Host "      $d"
}

# ---------------------------------------------------------------------------
# STEP 3 - Copy hook file (UTF-8 no BOM, LF endings preserved)
# ---------------------------------------------------------------------------
Write-Host "[3/8] Copying hook..." -ForegroundColor Yellow
$destHook = Join-Path $claudeHooks "tkk_read_guard.py"
$content = [System.IO.File]::ReadAllBytes($srcHook)
[System.IO.File]::WriteAllBytes($destHook, $content)
$srcSize = (Get-Item $srcHook).Length
$dstSize = (Get-Item $destHook).Length
if ($srcSize -ne $dstSize) {
    Write-Host ("ERROR: size mismatch src={0} dst={1}" -f $srcSize, $dstSize) -ForegroundColor Red
    exit 1
}
Write-Host ("      {0} ({1} bytes)" -f $destHook, $dstSize)

# ---------------------------------------------------------------------------
# STEP 4 - Write default config.json (preserve user edits)
# ---------------------------------------------------------------------------
Write-Host "[4/8] Writing config.json..." -ForegroundColor Yellow
$cfgPath = Join-Path $tkkHome "config.json"
if (Test-Path $cfgPath) {
    Write-Host "      $cfgPath already exists, preserving user edits."
} else {
    $defaultCfg = [ordered]@{
        enabled = $true
        overlap_block_threshold = 0.8
        small_read_exemption_lines = 100
        rule_b_max_limit = 1000
        override_sentinel = 999911
        override_replacement_limit = 2000
        always_fresh_patterns = @("*.log", "**/scratch/**", "**/tmp/**", "**/.tkk/**", "**/logs/**", "**/.atlas/03-active.md")
        always_fresh_age_seconds = 60
        state_retention_hours = 4
        state_retention_calls = 200
        lock_timeout_seconds = 5
        stale_lock_age_seconds = 30
        max_decisions_log_mb = 100
        max_errors_log_mb = 10
        log_decisions = $true
        case_insensitive_paths = $true
    }
    $json = $defaultCfg | ConvertTo-Json -Depth 4
    # UTF-8 no BOM
    [System.IO.File]::WriteAllText($cfgPath, $json, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "      $cfgPath"
}

# ---------------------------------------------------------------------------
# STEP 5 - Backup existing settings.json
# ---------------------------------------------------------------------------
Write-Host "[5/8] Backing up settings.json..." -ForegroundColor Yellow
$settingsPath = Join-Path $env:USERPROFILE ".claude\settings.json"
$backupPath = $null
if (Test-Path $settingsPath) {
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $backupPath = "$settingsPath.bak.$stamp"
    Copy-Item $settingsPath $backupPath -Force
    Write-Host "      backup: $backupPath"
} else {
    Write-Host "      (no existing settings.json - fresh install)"
}

# ---------------------------------------------------------------------------
# STEP 6 - Merge into settings.json (atomic, idempotent)
# ---------------------------------------------------------------------------
Write-Host "[6/8] Merging hook registration into settings.json..." -ForegroundColor Yellow
$settings = $null
if (Test-Path $settingsPath) {
    try {
        $raw = Get-Content $settingsPath -Raw -Encoding UTF8
        $settings = $raw | ConvertFrom-Json -AsHashtable
    } catch {
        Write-Host "ABORT: settings.json has invalid JSON (or contains comments). Clean it first." -ForegroundColor Red
        Write-Host ("       error: {0}" -f $_.Exception.Message)
        exit 1
    }
} else {
    $settings = @{}
}

if (-not $settings.ContainsKey("hooks") -or $null -eq $settings["hooks"]) {
    $settings["hooks"] = @{}
}
if (-not $settings["hooks"].ContainsKey("PreToolUse") -or $null -eq $settings["hooks"]["PreToolUse"]) {
    $settings["hooks"]["PreToolUse"] = @()
}

# Idempotency check
$already = $false
foreach ($entry in $settings["hooks"]["PreToolUse"]) {
    if ($null -eq $entry) { continue }
    if (-not $entry.ContainsKey("hooks")) { continue }
    foreach ($h in $entry["hooks"]) {
        if ($null -ne $h.command -and $h.command -match "tkk_read_guard\.py") {
            $already = $true; break
        }
    }
    if ($already) { break }
}

if ($already) {
    Write-Host "      TKK hook already registered - skipping merge (idempotent)." -ForegroundColor Green
} else {
    $newEntry = @{
        matcher = "Read"
        hooks = @(
            @{
                type = "command"
                command = ("python `"{0}`"" -f $destHook)
                timeout = 5
            }
        )
    }
    $settings["hooks"]["PreToolUse"] = @($settings["hooks"]["PreToolUse"]) + @($newEntry)

    # Atomic write
    $json = $settings | ConvertTo-Json -Depth 10
    $tmpPath = "$settingsPath.tmp"
    [System.IO.File]::WriteAllText($tmpPath, $json, (New-Object System.Text.UTF8Encoding($false)))
    Move-Item -Force $tmpPath $settingsPath
    Write-Host "      registered new PreToolUse(matcher=Read) entry."
}

# ---------------------------------------------------------------------------
# STEP 7 - Smoke test
# ---------------------------------------------------------------------------
Write-Host "[7/8] Smoke test..." -ForegroundColor Yellow
$smokePayload = @{
    session_id = "install-smoketest"
    tool_name = "Read"
    tool_input = @{ file_path = (Join-Path $env:TEMP "tkk_smoke_nonexistent.txt"); offset = 0; limit = 100 }
} | ConvertTo-Json -Depth 5 -Compress

$errLogBefore = if (Test-Path (Join-Path $tkkHome "errors.log")) { (Get-Item (Join-Path $tkkHome "errors.log")).Length } else { 0 }

$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = "python"
$psi.Arguments = "`"$destHook`""
$psi.RedirectStandardInput = $true
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$psi.UseShellExecute = $false
$proc = [System.Diagnostics.Process]::Start($psi)
$proc.StandardInput.Write($smokePayload)
$proc.StandardInput.Close()
$smokeOut = $proc.StandardOutput.ReadToEnd()
$smokeErr = $proc.StandardError.ReadToEnd()
$proc.WaitForExit(10000) | Out-Null
$smokeRc = $proc.ExitCode

if ($smokeRc -ne 0) {
    Write-Host ("WARN: smoke test exit code = {0} (expected 0)" -f $smokeRc) -ForegroundColor Yellow
    Write-Host "stderr: $smokeErr"
} else {
    Write-Host "      smoke test exit 0 OK"
}

$errLogAfter = if (Test-Path (Join-Path $tkkHome "errors.log")) { (Get-Item (Join-Path $tkkHome "errors.log")).Length } else { 0 }
if ($errLogAfter -gt $errLogBefore) {
    Write-Host "      NOTE: errors.log grew during smoke test - check $tkkHome\errors.log" -ForegroundColor Yellow
}

# Clean up smoke-test counter shard so it doesn't pollute real audits
$smokeCounter = Join-Path $tkkHome "counters\install-smoketest.json"
if (Test-Path $smokeCounter) { Remove-Item $smokeCounter -Force }
$smokeLog = Join-Path $tkkHome "read_log\install-smoketest.jsonl"
if (Test-Path $smokeLog) { Remove-Item $smokeLog -Force }

# ---------------------------------------------------------------------------
# STEP 8 - Summary
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "[8/8] Install complete." -ForegroundColor Green
Write-Host ""
Write-Host "  Hook:         $destHook"
Write-Host "  Config:       $cfgPath"
Write-Host "  State dir:    $tkkHome"
if ($backupPath) {
    Write-Host "  Backup:       $backupPath"
}
Write-Host ""
Write-Host "Verify: open Claude Code, read any file, then read the same file again - second should block." -ForegroundColor Cyan
Write-Host "Override:   pass limit=999911 in a Read call to force re-read."
Write-Host "Kill switch: '{`"enabled`": false}' | Set-Content `$env:USERPROFILE\.tkk\config.json"
Write-Host "Audit:       powershell -File $($PSScriptRoot -replace 'install$','hooks')\burn_audit.ps1"
Write-Host ""
