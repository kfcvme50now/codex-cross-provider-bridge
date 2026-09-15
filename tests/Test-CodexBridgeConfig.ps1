[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repositoryRoot = Split-Path $PSScriptRoot -Parent
$temporaryRoot = Join-Path $env:TEMP ("codex-cross-provider-bridge-test-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $temporaryRoot -Force | Out-Null
$env:CODEX_BRIDGE_BACKUP_ROOT = Join-Path $temporaryRoot "backups"

. (Join-Path $repositoryRoot "scripts\CodexCrossProviderBridge.Common.ps1")

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
    $firstHash = Get-Sha256Hex -Path $configPath

    & $repair -ConfigPath $configPath -BridgeUrl "http://127.0.0.1:15722/v1" | Out-Null
    $secondHash = Get-Sha256Hex -Path $configPath
    Assert-True ($firstHash -eq $secondHash) "repeat repair must be idempotent"

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

    $automationScript = Join-Path $repositoryRoot "scripts\Invoke-CodexCrossProviderAutomation.ps1"
    $automationStatus = Join-Path $temporaryRoot "automation-status.json"
    $automationConfig = Join-Path $temporaryRoot "automation-config.toml"
    @'
model_provider = "custom"
model = "deepseek-flash"

[model_providers.custom]
name = "Third Party"
base_url = "http://127.0.0.1:15721/v1"
wire_api = "responses"
requires_openai_auth = true
experimental_bearer_token = "PROXY_MANAGED"
'@ | Set-Content -LiteralPath $automationConfig -Encoding utf8

    & $automationScript `
        -Apply `
        -ConfigPath $automationConfig `
        -HistoryScope none `
        -SkipHistoryMigration `
        -StatusFile $automationStatus | Out-Null
    $automationResult = Get-Content -Raw -LiteralPath $automationConfig
    Assert-True ($automationResult -match 'http://127\.0\.0\.1:15722/v1') "automatic route repair did not use bridge"

    $officialConfig = Join-Path $temporaryRoot "official-config.toml"
    @'
model_provider = "openai"
model = "gpt-5.5"

[model_providers.openai]
name = "OpenAI"
'@ | Set-Content -LiteralPath $officialConfig -Encoding utf8
    $officialHash = Get-Sha256Hex -Path $officialConfig
    & $automationScript `
        -Apply `
        -ConfigPath $officialConfig `
        -HistoryScope none `
        -SkipHistoryMigration `
        -StatusFile $automationStatus | Out-Null
    $officialHashAfter = Get-Sha256Hex -Path $officialConfig
    Assert-True ($officialHash -eq $officialHashAfter) "official GPT route must not be modified"

    $fakeProbe = Join-Path $temporaryRoot "fake-probe.py"
    @'
import json

print(json.dumps({"type": "thread.started", "thread_id": "probe-thread"}))
print(json.dumps({"type": "turn.completed"}))
'@ | Set-Content -LiteralPath $fakeProbe -Encoding utf8
    $fakeCodex = Join-Path $temporaryRoot "fake-codex.cmd"
    "@echo off`r`npython `"$fakeProbe`" %*" | Set-Content -LiteralPath $fakeCodex -Encoding ascii
    $probePolicy = Join-Path $temporaryRoot "policy.json"
    @'
{
  "scope": "all",
  "historyScope": "all",
  "conversationIds": [],
  "armed": false,
  "targetConversationId": "",
  "updatedAt": 1
}
'@ | Set-Content -LiteralPath $probePolicy -Encoding utf8
    $probeState = Join-Path $temporaryRoot "probe-state.json"
    $probeStatus = Join-Path $temporaryRoot "probe-status.json"
    $env:CODEX_EXECUTABLE = $fakeCodex
    try {
        $probeOutput = & $automationScript `
            -Apply `
            -ConfigPath $automationConfig `
            -HistoryScope none `
            -SkipHistoryMigration `
            -PostSwitchProbeMode cli `
            -PostSwitchScope next `
            -ProbeStateFile $probeState `
            -ProbeStatusFile $probeStatus `
            -StatusFile $automationStatus
    } finally {
        Remove-Item Env:CODEX_EXECUTABLE -ErrorAction SilentlyContinue
    }
    $probeText = $probeOutput -join "`n"
    Assert-True ($probeText -match '(?m)^post_switch_probe_status=probed$') "post-switch probe did not run"
    Assert-True ($probeText -match '(?m)^post_switch_probe_ok=True$') "post-switch probe did not succeed"
    Assert-True ($probeText -match '(?m)^post_switch_policy_status=applied$') "post-switch policy was not applied"
    $updatedProbePolicy = Get-Content -Raw -LiteralPath $probePolicy | ConvertFrom-Json
    Assert-True ($updatedProbePolicy.scope -eq "next") "post-switch policy did not arm next conversation"

    $lifecycleManager = Join-Path $repositoryRoot "scripts\Manage-CodexLifecycleHooks.ps1"
    $lifecycleCodexHome = Join-Path $temporaryRoot ".codex-lifecycle"
    New-Item -ItemType Directory -Path $lifecycleCodexHome -Force | Out-Null
    $lifecyclePolicy = Join-Path $temporaryRoot "lifecycle-policy.json"
    $lifecycleStatus = Join-Path $temporaryRoot "lifecycle-status.json"
    & $lifecycleManager `
        -Action set-policy `
        -CodexHome $lifecycleCodexHome `
        -ConfigPath $officialConfig `
        -PolicyPath $lifecyclePolicy `
        -StatusFile $lifecycleStatus `
        -CompactMode repair-and-stop `
        -AutoBranch false `
        -BranchBackend app-server `
        -PostSwitchProbeMode disabled `
        -PostSwitchScope preserve `
        -SessionStartMode repair `
        -Apply | Out-Null
    & $lifecycleManager `
        -Action set-policy `
        -CodexHome $lifecycleCodexHome `
        -ConfigPath $officialConfig `
        -PolicyPath $lifecyclePolicy `
        -StatusFile $lifecycleStatus `
        -CompactMode block-only `
        -AutoBranch false `
        -Apply | Out-Null
    $policyBackups = & $lifecycleManager `
        -Action list-policy-backups `
        -CodexHome $lifecycleCodexHome `
        -ConfigPath $officialConfig `
        -PolicyPath $lifecyclePolicy `
        -StatusFile $lifecycleStatus | ConvertFrom-Json
    Assert-True (@($policyBackups).Count -ge 1) "lifecycle policy backup was not created"
    & $lifecycleManager `
        -Action restore-policy-backup `
        -CodexHome $lifecycleCodexHome `
        -ConfigPath $officialConfig `
        -PolicyPath $lifecyclePolicy `
        -StatusFile $lifecycleStatus `
        -BackupFile $policyBackups[0].backupFile `
        -Apply | Out-Null
    Assert-True ((Get-Content -Raw -LiteralPath $lifecyclePolicy | ConvertFrom-Json).compactRepairMode -eq "repair-and-stop") "lifecycle policy backup restore failed"
    $installedLifecycle = & $lifecycleManager `
        -Action install-hooks `
        -CodexHome $lifecycleCodexHome `
        -ConfigPath $officialConfig `
        -PolicyPath $lifecyclePolicy `
        -StatusFile $lifecycleStatus `
        -Apply | ConvertFrom-Json
    Assert-True ($installedLifecycle.status -eq "installed") "lifecycle hooks were not installed"
    $hooksPath = Join-Path $lifecycleCodexHome "hooks.json"
    $hooksText = Get-Content -Raw -LiteralPath $hooksPath
    Assert-True ($hooksText -match 'PreCompact') "PreCompact hook missing"
    Assert-True ($hooksText -match 'SessionStart') "SessionStart hook missing"
    $lifecycleState = & $lifecycleManager `
        -Action status `
        -CodexHome $lifecycleCodexHome `
        -PolicyPath $lifecyclePolicy `
        -StatusFile $lifecycleStatus | ConvertFrom-Json
    Assert-True ($lifecycleState.hooks.managedHooksInstalled) "managed hook status missing"
    Assert-True ($lifecycleState.policy.compactRepairMode -eq "repair-and-stop") "policy mode missing"
    & $lifecycleManager `
        -Action uninstall-hooks `
        -CodexHome $lifecycleCodexHome `
        -PolicyPath $lifecyclePolicy `
        -StatusFile $lifecycleStatus `
        -Apply | Out-Null
    $hooksAfterUninstall = Get-Content -Raw -LiteralPath $hooksPath
    Assert-True ($hooksAfterUninstall -notmatch 'codex_lifecycle_hook') "managed hook survived uninstall"
    & $lifecycleManager `
        -Action restore-hooks `
        -CodexHome $lifecycleCodexHome `
        -PolicyPath $lifecyclePolicy `
        -StatusFile $lifecycleStatus `
        -BackupDirectory $installedLifecycle.backupDirectory `
        -Apply | Out-Null
    Assert-True (-not (Test-Path -LiteralPath $hooksPath)) "hooks.json was not restored to its original absent state"

    $bridgeManager = Join-Path $repositoryRoot "scripts\Manage-CodexCrossProviderBridge.ps1"
    $managedCodexHome = Join-Path $temporaryRoot ".codex-managed"
    $managedState = Join-Path $temporaryRoot "bridge-state"
    New-Item -ItemType Directory -Path $managedCodexHome -Force | Out-Null
    New-Item -ItemType Directory -Path $managedState -Force | Out-Null
    & $bridgeManager `
        -Action enable-compact-hook `
        -CodexHome $managedCodexHome `
        -ConfigPath $officialConfig `
        -BridgeStateDirectory $managedState `
        -CompactHookMode repair-and-stop `
        -SessionStartMode repair `
        -PostSwitchProbeMode disabled `
        -PostSwitchScope preserve `
        -BranchBackend app-server `
        -ApplyOperation | Out-Null
    $managedHooks = Join-Path $managedCodexHome "hooks.json"
    $managedPolicy = Join-Path $managedState "lifecycle-policy.json"
    Assert-True (Test-Path -LiteralPath $managedHooks) "manager did not install hooks into the selected CODEX_HOME"
    Assert-True ((Get-Content -Raw -LiteralPath $managedPolicy | ConvertFrom-Json).compactRepairMode -eq "repair-and-stop") "manager did not persist the selected compact mode"
    & $bridgeManager `
        -Action disable-compact-hook `
        -CodexHome $managedCodexHome `
        -ConfigPath $officialConfig `
        -BridgeStateDirectory $managedState | Out-Null
    Assert-True ((Get-Content -Raw -LiteralPath $managedHooks) -notmatch 'codex_lifecycle_hook') "manager did not remove its managed hooks"

    Write-Output "status=passed"
    Write-Output "tests=historical_alias,new_provider_metadata,idempotency,backup_restore,input_validation,automation_guard,post_switch_probe,lifecycle_hook_backup_restore,manager_compact_hook"
} finally {
    Remove-Item Env:CODEX_BRIDGE_BACKUP_ROOT -ErrorAction SilentlyContinue
    $resolvedTemporaryRoot = [System.IO.Path]::GetFullPath($temporaryRoot)
    $resolvedTemp = [System.IO.Path]::GetFullPath($env:TEMP)
    if ($resolvedTemporaryRoot.StartsWith($resolvedTemp, [System.StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $resolvedTemporaryRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
