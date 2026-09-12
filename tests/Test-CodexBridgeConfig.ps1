[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repositoryRoot = Split-Path $PSScriptRoot -Parent
$temporaryRoot = Join-Path $env:TEMP ("codex-cross-provider-bridge-test-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $temporaryRoot -Force | Out-Null
$env:CODEX_BRIDGE_BACKUP_ROOT = Join-Path $temporaryRoot "backups"

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) {
        throw "Assertion failed: $Message"
    }
}

try {
    $configPath = Join-Path $temporaryRoot "config.toml"
    @'
model_provider = "custom"
model = "example-model"

[model_providers.custom]
name = "Example"
base_url = "http://127.0.0.1:15721/v1"
wire_api = "responses"
requires_openai_auth = true
experimental_bearer_token = "PROXY_MANAGED"
'@ | Set-Content -LiteralPath $configPath -Encoding utf8

    $repair = Join-Path $repositoryRoot "scripts\Repair-Codex-CCSwitchProviderAlias.ps1"
    $bridgeScript = Join-Path $repositoryRoot "src\codex_cross_provider_bridge.py"

    & $repair -ConfigPath $configPath -BridgeUrl "http://127.0.0.1:15722/v1" | Out-Null
    $first = Get-Content -Raw -LiteralPath $configPath
    Assert-True ($first -match '(?m)^\[model_providers\.custom\]') "custom provider missing"
    Assert-True ($first -match '(?m)^\[model_providers\.cc-switch-official\]') "official alias missing"
    Assert-True (($first | Select-String -Pattern '15722/v1' -AllMatches).Matches.Count -eq 2) "both providers must use the bridge"
    $firstHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $configPath).Hash

    & $repair -ConfigPath $configPath -BridgeUrl "http://127.0.0.1:15722/v1" | Out-Null
    $secondHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $configPath).Hash
    Assert-True ($firstHash -eq $secondHash) "repeat repair must be idempotent"

    . (Join-Path $repositoryRoot "scripts\CodexCrossProviderBridge.Common.ps1")
    $snapshot = New-CodexBridgeSnapshot `
        -Reason "manual" `
        -ConfigPath $configPath `
        -BridgeScript $bridgeScript

    @'
model_provider = "custom"

[model_providers.custom]
name = "Broken state during test"
base_url = "http://127.0.0.1:15721/v1"
wire_api = "responses"
'@ | Set-Content -LiteralPath $configPath -Encoding utf8

    Restore-CodexBridgeSnapshot -SnapshotDirectory $snapshot.Directory -KeepCurrentTask | Out-Null
    $restored = Get-Content -Raw -LiteralPath $configPath
    Assert-True ($restored -match '(?m)^\[model_providers\.cc-switch-official\]') "restore lost official alias"
    Assert-True ($restored -match 'http://127\.0\.0\.1:15722/v1') "restore lost bridge URL"

    $invalidRejected = $false
    try {
        & $repair -ConfigPath $configPath -BridgeUrl "http://127.0.0.1:15721/v2" | Out-Null
    } catch {
        $invalidRejected = $true
    }
    Assert-True $invalidRejected "invalid bridge URL must be rejected"

    Write-Output "status=passed"
    Write-Output "tests=historical_alias,new_provider_metadata,idempotency,backup_restore,input_validation"
} finally {
    Remove-Item Env:CODEX_BRIDGE_BACKUP_ROOT -ErrorAction SilentlyContinue
    $resolvedTemporaryRoot = [System.IO.Path]::GetFullPath($temporaryRoot)
    $resolvedTemp = [System.IO.Path]::GetFullPath($env:TEMP)
    if ($resolvedTemporaryRoot.StartsWith($resolvedTemp, [System.StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $resolvedTemporaryRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
