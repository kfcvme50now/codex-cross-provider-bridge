[CmdletBinding()]
param(
    [string]$ConfigPath = "$env:USERPROFILE\.codex\config.toml",
    [string]$LegacyProviderId = "cc-switch-official",
    [string]$BridgeUrl = ""
)

. (Join-Path $PSScriptRoot "CodexCrossProviderBridge.Common.ps1")

if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
    throw "Codex config not found: $ConfigPath"
}

$raw = [System.IO.File]::ReadAllText($ConfigPath)
$updated = $raw
$legacyIdEscaped = [regex]::Escape($LegacyProviderId)

if ($BridgeUrl) {
    if ($BridgeUrl -notmatch '^http://127\.0\.0\.1:\d+/v1$') {
        throw "BridgeUrl must be a loopback http:// URL ending in /v1"
    }
    $updated = $updated -replace (
        'base_url\s*=\s*"http://127\.0\.0\.1:15721/v1"'
    ), "base_url = `"$BridgeUrl`""
}

$aliasExists = $updated -match "(?m)^\[model_providers\.$legacyIdEscaped\]"
if ($aliasExists -and $updated -eq $raw) {
    Write-Output "status=already_present"
    Write-Output "provider_id=$LegacyProviderId"
    exit 0
}

if (-not $aliasExists) {
    $activeProviderMatch = [regex]::Match(
        $updated,
        '(?m)^model_provider\s*=\s*"([^"]+)"'
    )
    $activeProviderId = if ($activeProviderMatch.Success) {
        $activeProviderMatch.Groups[1].Value
    } else {
        ""
    }

    $source = $null
    if ($activeProviderId -and $activeProviderId -ne $LegacyProviderId) {
        $activeIdEscaped = [regex]::Escape($activeProviderId)
        $source = [regex]::Match(
            $updated,
            "(?ms)^\[model_providers\.$activeIdEscaped\]\s*\r?\n.*?(?=^\[|\z)"
        )
    }
    if (-not $source.Success) {
        $source = [regex]::Match(
            $updated,
            '(?ms)^\[model_providers\.custom\]\s*\r?\n.*?(?=^\[|\z)'
        )
    }
    if (-not $source.Success) {
        $source = [regex]::Match(
            $updated,
            '(?ms)^\[model_providers\.cc-switch-official\]\s*\r?\n.*?(?=^\[|\z)'
        )
    }

    if ($source.Success) {
        $block = $source.Value.TrimEnd("`r", "`n")
        $sourceHeader = [regex]::Match($block, '^\[model_providers\.[^]]+\]').Value
        $alias = $block -replace [regex]::Escape($sourceHeader), "[model_providers.$LegacyProviderId]"
    } else {
        $alias = @"
[model_providers.$LegacyProviderId]
name = "OpenAI legacy alias"
wire_api = "responses"
requires_openai_auth = true
"@
    }

    $insert = @"
# Compatibility alias for Codex threads persisted under a previous CC Switch route.
$alias

"@

    if ($source.Success) {
        $updated = $updated.Substring(0, $source.Index) + $insert + $updated.Substring($source.Index)
    } else {
        $updated = $updated.TrimEnd() + "`r`n`r`n" + $insert
    }
}

$snapshot = New-CodexBridgeSnapshot `
    -Reason "pre-repair" `
    -ConfigPath $ConfigPath `
    -BridgeScript (Join-Path (Split-Path $PSScriptRoot -Parent) "src\codex_cross_provider_bridge.py")

$temporaryPath = "$ConfigPath.cc-switch-alias.$PID.tmp"
Write-Utf8NoBom -Path $temporaryPath -Content $updated

try {
    if (-not (Test-TomlFile -Path $temporaryPath)) {
        throw "Temporary config is not valid TOML"
    }
    Move-Item -LiteralPath $temporaryPath -Destination $ConfigPath -Force
} catch {
    if (Test-Path -LiteralPath $temporaryPath) {
        Remove-Item -LiteralPath $temporaryPath -Force
    }
    Restore-CodexBridgeSnapshot -SnapshotDirectory $snapshot.Directory -KeepCurrentTask | Out-Null
    throw
}

Write-Output "status=repaired"
Write-Output "provider_id=$LegacyProviderId"
if ($BridgeUrl) {
    Write-Output "bridge_url=$BridgeUrl"
}
Write-Output "snapshot_id=$($snapshot.SnapshotId)"
