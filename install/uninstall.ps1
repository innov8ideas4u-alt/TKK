# uninstall.ps1 - TKK Phase 1 Read Guard uninstaller
# Restores most-recent settings.json.bak.<timestamp> backup OR removes our
# entry surgically if no backup exists. Removes hook file. Leaves ~/.tkk state
# alone by default (use -PurgeState to wipe).

[CmdletBinding()]
param(
    [switch]$PurgeState,
    [switch]$KeepHookFile
)

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "=== TKK Phase 1 Read Guard - Uninstaller ===" -ForegroundColor Cyan
Write-Host ""

$claudeDir = Join-Path $env:USERPROFILE ".claude"
$settingsPath = Join-Path $claudeDir "settings.json"
$destHook = Join-Path $claudeDir "hooks\tkk_read_guard.py"
$tkkHome = Join-Path $env:USERPROFILE ".tkk"

# Find latest backup
$backups = @()
if (Test-Path $claudeDir) {
    $backups = Get-ChildItem -Path $claudeDir -Filter "settings.json.bak.*" -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending
}

if ($backups.Count -gt 0) {
    $latest = $backups[0].FullName
    Write-Host "Restoring settings.json from: $latest" -ForegroundColor Yellow
    Copy-Item $latest $settingsPath -Force
} else {
    Write-Host "No backup found - surgically removing TKK hook entry from settings.json" -ForegroundColor Yellow
    if (Test-Path $settingsPath) {
        try {
            $settings = Get-Content $settingsPath -Raw -Encoding UTF8 | ConvertFrom-Json -AsHashtable
        } catch {
            Write-Host "ABORT: settings.json has invalid JSON." -ForegroundColor Red
            exit 1
        }
        if ($settings.ContainsKey("hooks") -and $settings["hooks"].ContainsKey("PreToolUse")) {
            $filtered = @()
            foreach ($entry in $settings["hooks"]["PreToolUse"]) {
                $isTkk = $false
                if ($null -ne $entry -and $entry.ContainsKey("hooks")) {
                    foreach ($h in $entry["hooks"]) {
                        if ($null -ne $h.command -and $h.command -match "tkk_read_guard\.py") {
                            $isTkk = $true; break
                        }
                    }
                }
                if (-not $isTkk) { $filtered += $entry }
            }
            $settings["hooks"]["PreToolUse"] = $filtered
            $json = $settings | ConvertTo-Json -Depth 10
            [System.IO.File]::WriteAllText($settingsPath, $json, (New-Object System.Text.UTF8Encoding($false)))
        }
    }
}

# Remove hook file
if (-not $KeepHookFile -and (Test-Path $destHook)) {
    Remove-Item $destHook -Force
    Write-Host "Removed hook file: $destHook"
}

# Optionally purge state
if ($PurgeState -and (Test-Path $tkkHome)) {
    Write-Host "Purging state dir: $tkkHome" -ForegroundColor Yellow
    Remove-Item $tkkHome -Recurse -Force
} else {
    Write-Host "State dir kept at $tkkHome (use -PurgeState to remove)."
}

Write-Host ""
Write-Host "Uninstall complete." -ForegroundColor Green
