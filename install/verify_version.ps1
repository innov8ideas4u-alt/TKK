# verify_version.ps1 - Check claude CLI version meets TKK requirement
# Required: claude >= 2.0.10 (PreToolUse stdout mutation API)
# Exit 0 on OK, 1 on failure. Intended to be sourced or called from install.ps1.

[CmdletBinding()]
param(
    [string]$MinVersion = "2.0.10"
)

$ErrorActionPreference = "Stop"

function Parse-SemVer([string]$s) {
    if (-not $s) { return $null }
    $m = [regex]::Match($s, '(\d+)\.(\d+)\.(\d+)')
    if (-not $m.Success) { return $null }
    return [Version]::new([int]$m.Groups[1].Value, [int]$m.Groups[2].Value, [int]$m.Groups[3].Value)
}

try {
    $raw = & claude --version 2>&1 | Out-String
} catch {
    Write-Host "ERROR: 'claude' CLI not found on PATH." -ForegroundColor Red
    exit 1
}

$found = Parse-SemVer $raw
if ($null -eq $found) {
    Write-Host "ERROR: could not parse claude version from output: $raw" -ForegroundColor Red
    exit 1
}

$min = Parse-SemVer $MinVersion
if ($found -lt $min) {
    Write-Host ("ERROR: claude {0} is older than required {1}. PreToolUse mutation API requires >= {1}." -f $found, $MinVersion) -ForegroundColor Red
    exit 1
}

Write-Host ("OK: claude {0} >= {1}" -f $found, $MinVersion) -ForegroundColor Green
exit 0
