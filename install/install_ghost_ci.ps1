# TKK Phase 2 — Ghost CI installer
# Idempotent. Requires PowerShell 7+ (pwsh). Run from a project root.

$ErrorActionPreference = "Stop"

# 1. Idempotency: ensure TKK Phase 1 exists
$TkkDir = "$env:USERPROFILE\.tkk"
if (-Not (Test-Path $TkkDir)) {
    Write-Error "TKK Phase 1 not found at $TkkDir. Install TKK Phase 1 first."
    exit 1
}

# 2. PowerShell 7+ guard
if ($PSVersionTable.PSVersion.Major -lt 7) {
    Write-Error "Ghost CI installer requires PowerShell 7+ (pwsh)."
    exit 1
}

# 3. PRE-FLIGHT: Verify Ollama on port 11535
Write-Host "Verifying local Ollama endpoint on port 11535..."
try {
    $response = Invoke-RestMethod -Uri "http://localhost:11535/api/tags" -Method Get -TimeoutSec 5 -ErrorAction Stop
    $hasModel = $response.models | Where-Object { $_.name -like "llama3.1:8b-instruct-q8_0*" }
    if (-not $hasModel) {
        Write-Error "PRE-FLIGHT FAILED: llama3.1:8b-instruct-q8_0 not pulled."
        Write-Error "Run: ollama pull llama3.1:8b-instruct-q8_0"
        exit 1
    }
    Write-Host "  Ollama OK on :11535, llama3.1:8b-instruct-q8_0 available."
} catch {
    Write-Error "PRE-FLIGHT FAILED: Ollama not accessible on http://localhost:11535/api/tags"
    Write-Error "Ensure Ollama is running with OLLAMA_HOST=0.0.0.0:11535"
    exit 1
}

# 4. Install Python deps
Write-Host "Installing Python dependencies..."
python -m pip install --user watchdog==4.0.0 aiohttp==3.9.3 psutil==5.9.8 colorama==0.4.6 pytest-testmon==2.1.1
if ($LASTEXITCODE -ne 0) {
    Write-Error "pip install failed."
    exit 1
}

# 5. Verify model resident (advisory)
$ollamaPs = Invoke-RestMethod -Uri "http://localhost:11535/api/ps" -ErrorAction SilentlyContinue
if ($null -eq $ollamaPs -or -not ($ollamaPs.models | Where-Object { $_.name -like "llama3.1:8b*" })) {
    Write-Warning "Llama 3.1 8B not currently resident. Run: ollama run llama3.1:8b-instruct-q8_0"
}

# 6. Deploy daemon to ~/.tkk/ghost_ci/
$GhostDir = "$TkkDir\ghost_ci"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$SourceDir = Join-Path $RepoRoot "ghost_ci"
if (-Not (Test-Path $SourceDir)) {
    Write-Error "Source directory not found: $SourceDir"
    exit 1
}
New-Item -ItemType Directory -Force -Path $GhostDir | Out-Null
Copy-Item "$SourceDir\*" -Destination $GhostDir -Recurse -Force -Exclude "tests","__pycache__"
Write-Host "  Deployed daemon to $GhostDir"

# 7. Create config.json with default kill-switch
$ConfigPath = "$GhostDir\config.json"
if (-not (Test-Path $ConfigPath)) {
    @{
        ghost_ci_enabled = $true
        debounce_seconds = 1.5
        pytest_timeout_seconds = 15
        ollama_url = "http://localhost:11535"
        distillation_timeout_seconds = 3.0
    } | ConvertTo-Json | Set-Content -Path $ConfigPath -Encoding UTF8
    Write-Host "  Created default config at $ConfigPath"
}

# 8. Add Ghost CI cache paths to project .gitignore
$ProjectGitignore = "$pwd\.gitignore"
$GhostIgnores = @(
    "# Ghost CI cache isolation (TKK Phase 2)",
    ".ghost_pytest_cache/",
    ".testmondata",
    ".atlas/00-urgent-alerts.md.lock/",
    ".atlas/ghost_ci.pid"
)
if (Test-Path $ProjectGitignore) {
    $existing = Get-Content $ProjectGitignore -Raw
    if ($existing -notmatch ".ghost_pytest_cache") {
        Add-Content -Path $ProjectGitignore -Value "`n$($GhostIgnores -join "`n")"
        Write-Host "  Added Ghost CI ignores to $ProjectGitignore"
    }
}

# 9. Register SessionStart hook
$SettingsPath = "$env:USERPROFILE\.claude\settings.json"
if (-Not (Test-Path $SettingsPath)) {
    Write-Error "Phase 1 settings.json not found at $SettingsPath"
    exit 1
}
$BackupPath = "$SettingsPath.bak.ghost_ci.$(Get-Date -Format 'yyyyMMdd_HHmmss')"
Copy-Item $SettingsPath $BackupPath
Write-Host "  Backup: $BackupPath"

$settings = Get-Content $SettingsPath -Raw | ConvertFrom-Json -AsHashtable
if (-not $settings.hooks) { $settings.hooks = @{} }
if (-not $settings.hooks.SessionStart) { $settings.hooks.SessionStart = @() }
$existingGhost = $settings.hooks.SessionStart | Where-Object { $_.command -like "*spawn_ghost*" }
if ($null -eq $existingGhost) {
    $settings.hooks.SessionStart += @{
        type = "command"
        command = "python `"$GhostDir\spawn_ghost.py`""
        timeout = 5
    }
    Write-Host "  Registered SessionStart hook"
} else {
    Write-Host "  SessionStart hook already present (idempotent)"
}
$settings | ConvertTo-Json -Depth 10 |
    Set-Content -Path "$SettingsPath.tmp" -Encoding UTF8 -NoNewline
Move-Item -Force "$SettingsPath.tmp" $SettingsPath

# 10. Inject CLAUDE.md directive (project-scoped)
$claudeMd = "$pwd\CLAUDE.md"
$directive = @"

# CRITICAL SYSTEM DIRECTIVE: PRE-EMPTIVE VALIDATION
Before invoking ANY tool, generating ANY code, or formulating ANY plan,
you MUST read ``.atlas/00-urgent-alerts.md``.
If this file contains active failures, you are strictly forbidden from
working on new features. Your sole objective is to debug and clear the alerts.
"@
if ((Test-Path $claudeMd) -and -not ((Get-Content $claudeMd -Raw) -match "00-urgent-alerts")) {
    Add-Content -Path $claudeMd -Value $directive
    Write-Host "  Injected Ghost CI directive into $claudeMd"
}

# 11. Smoke test
Write-Host "Smoke testing daemon spawn..."
& python "$GhostDir\spawn_ghost.py" --test-mode
if ($LASTEXITCODE -ne 0) {
    Write-Error "Smoke test failed. Restore from $BackupPath"
    exit 1
}

Write-Host ""
Write-Host "Ghost CI installed. It will auto-launch on your next cc session."
Write-Host "Kill switch: edit $ConfigPath -> ghost_ci_enabled: false"
