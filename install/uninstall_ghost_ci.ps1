# TKK Phase 2 — Ghost CI uninstaller

$ErrorActionPreference = "Continue"

$TkkDir = "$env:USERPROFILE\.tkk"
$GhostDir = "$TkkDir\ghost_ci"
$SettingsPath = "$env:USERPROFILE\.claude\settings.json"

# 1. Kill any running daemon
Write-Host "Stopping any running Ghost CI daemons..."
Get-Process python -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*ghost_ci*daemon*" } |
    ForEach-Object { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue }

# 2. Restore most recent settings.json backup
$backups = Get-ChildItem "$SettingsPath.bak.ghost_ci.*" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending
if ($backups -and $backups.Count -gt 0) {
    $latest = $backups[0]
    Copy-Item $latest.FullName $SettingsPath -Force
    Write-Host "  Restored settings.json from $($latest.Name)"
} else {
    Write-Warning "No ghost_ci backup found. Removing SessionStart hook entries manually..."
    if (Test-Path $SettingsPath) {
        $settings = Get-Content $SettingsPath -Raw | ConvertFrom-Json -AsHashtable
        if ($settings.hooks.SessionStart) {
            $settings.hooks.SessionStart = @($settings.hooks.SessionStart |
                Where-Object { $_.command -notlike "*spawn_ghost*" })
            $settings | ConvertTo-Json -Depth 10 |
                Set-Content -Path $SettingsPath -Encoding UTF8 -NoNewline
        }
    }
}

# 3. Remove ~/.tkk/ghost_ci/
if (Test-Path $GhostDir) {
    Remove-Item -Recurse -Force $GhostDir
    Write-Host "  Removed $GhostDir"
}

# 4. Prompt CLAUDE.md removal
$claudeMd = "$pwd\CLAUDE.md"
if ((Test-Path $claudeMd) -and ((Get-Content $claudeMd -Raw) -match "00-urgent-alerts")) {
    $ans = Read-Host "Remove Ghost CI directive from CLAUDE.md? (y/N)"
    if ($ans -eq "y") {
        $raw = Get-Content $claudeMd -Raw
        $cleaned = $raw -replace "(?s)\r?\n# CRITICAL SYSTEM DIRECTIVE: PRE-EMPTIVE VALIDATION.*?clear the alerts\.\r?\n?", ""
        Set-Content -Path $claudeMd -Value $cleaned -Encoding UTF8 -NoNewline
        Write-Host "  Removed directive from $claudeMd"
    }
}

Write-Host ""
Write-Host "Ghost CI uninstalled. Phase 1 hooks (Bash/Read) preserved."
