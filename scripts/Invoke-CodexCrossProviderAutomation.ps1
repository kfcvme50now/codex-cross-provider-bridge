[CmdletBinding()]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute(
    "PSReviewUnusedParameter",
    "",
    Justification = "Parameters are consumed by nested functions in this script."
)]
param(
    [string]$CodexHome = (Join-Path $env:USERPROFILE ".codex"),
    [string]$ConfigPath = "$env:USERPROFILE\.codex\config.toml",
    [int]$BridgePort = 15722,
    [ValidateSet("none", "all", "conversation")]
    [string]$HistoryScope = "all",
    [string]$ConversationId = "",
    [string]$TargetProvider = "custom",
    [ValidateRange(0, [int]::MaxValue)]
    [int]$IdleSeconds = 300,
    [ValidateRange(1, [int]::MaxValue)]
    [int]$MaxMigrationsPerRun = 10,
    [ValidateSet("disabled", "cli", "app-server")]
    [string]$PostSwitchProbeMode = "disabled",
    [ValidateSet("preserve", "next", "all")]
    [string]$PostSwitchScope = "preserve",
    [ValidateRange(1, 300)]
    [int]$ProbeTimeoutSeconds = 30,
    [string]$StatusFile = "",
    [string]$ProbeStateFile = "",
    [string]$ProbeStatusFile = "",
    [switch]$IncludeCurrentConversation,
    [switch]$Apply,
    [switch]$SkipConfigRepair,
    [switch]$SkipHistoryMigration
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "CodexCrossProviderBridge.Common.ps1")

if (-not $StatusFile) {
    $StatusFile = Join-Path (Split-Path $PSScriptRoot -Parent) "state\automation-status.json"
}

$AutomationRoot = Split-Path $StatusFile -Parent
$LockPath = Join-Path $AutomationRoot "automation.lock"
$ConfigGuardScript = Join-Path (Split-Path $PSScriptRoot -Parent) "src\codex_config_guard.py"
$AutomationScript = Join-Path (Split-Path $PSScriptRoot -Parent) "src\codex_provider_automation.py"
$AuditScript = Join-Path (Split-Path $PSScriptRoot -Parent) "src\codex_history_audit.py"
$ProbeScript = Join-Path (Split-Path $PSScriptRoot -Parent) "src\codex_provider_probe.py"
$RepairScript = Join-Path $PSScriptRoot "Repair-Codex-CCSwitchProviderAlias.ps1"
$BridgeUrl = "http://127.0.0.1:$BridgePort/v1"
$RuntimeStatusFile = Join-Path (Split-Path $StatusFile -Parent) "status.json"
$PolicyFile = Join-Path (Split-Path $StatusFile -Parent) "policy.json"
if (-not $ProbeStateFile) {
    $ProbeStateFile = Join-Path (Split-Path $StatusFile -Parent) "provider-probe-state.json"
}
if (-not $ProbeStatusFile) {
    $ProbeStatusFile = Join-Path (Split-Path $StatusFile -Parent) "provider-probe-status.json"
}

New-Item -ItemType Directory -Path $AutomationRoot -Force | Out-Null

function Get-PythonExecutable {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) {
        throw "Python 3.10+ is required but python was not found on PATH"
    }
    return $python.Source
}

function Read-ConfigGuard {
    $python = Get-PythonExecutable
    $output = & $python $ConfigGuardScript --config $ConfigPath
    if ($LASTEXITCODE -ne 0) {
        throw "Config guard failed"
    }
    return $output | ConvertFrom-Json
}

function Write-AutomationStatus {
    param([Parameter(Mandatory)][object]$Payload)

    $temporary = "$StatusFile.$PID.tmp"
    $Payload | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $temporary -Encoding utf8
    Move-Item -LiteralPath $temporary -Destination $StatusFile -Force
}

function Resolve-ProviderRepairTarget {
    param([Parameter(Mandatory)][object]$Guard)

    $ids = New-Object System.Collections.Generic.List[string]
    if ($Guard.activeProvider) {
        $ids.Add([string]$Guard.activeProvider)
    }
    if ($HistoryScope -eq "all") {
        $ids.Add("cc-switch-official")
        $ids.Add("custom")
    }
    if ($HistoryScope -eq "conversation" -and $ConversationId) {
        $python = Get-PythonExecutable
        $auditOutput = & $python $AuditScript `
            --config $ConfigPath `
            --scope conversation `
            --conversation-id $ConversationId
        if ($LASTEXITCODE -ne 0) {
            throw "Conversation audit failed"
        }
        $audit = $auditOutput | ConvertFrom-Json
        foreach ($missingId in @($audit.missingProviderIds)) {
            if ($missingId) {
                $ids.Add([string]$missingId)
            }
        }
    }
    return @($ids | Sort-Object -Unique)
}

function Invoke-ConfigRepair {
    param([Parameter(Mandatory)][object]$Guard)

    if ($SkipConfigRepair) {
        return [pscustomobject]@{
            Status = "skipped-by-request"
            ProviderIds = @()
            Error = ""
        }
    }
    if (-not $Guard.eligibleForAutomaticBridgeRepair) {
        return [pscustomobject]@{
            Status = "skipped-$($Guard.reason)"
            ProviderIds = @()
            Error = ""
        }
    }

    $providerIds = @(Resolve-ProviderRepairTarget -Guard $Guard)
    if (@($providerIds).Count -eq 0) {
        return [pscustomobject]@{
            Status = "skipped-no-provider"
            ProviderIds = @()
            Error = ""
        }
    }

    if (-not $Apply) {
        return [pscustomobject]@{
            Status = "planned"
            ProviderIds = $providerIds
            Error = ""
        }
    }

    $beforeHash = Get-Sha256Hex -Path $ConfigPath
    Start-Sleep -Milliseconds 400
    $stableHash = Get-Sha256Hex -Path $ConfigPath
    if ($beforeHash -ne $stableHash) {
        return [pscustomobject]@{
            Status = "deferred-config-changing"
            ProviderIds = $providerIds
            Error = ""
        }
    }

    foreach ($providerId in $providerIds) {
        & $RepairScript `
            -ConfigPath $ConfigPath `
            -LegacyProviderId $providerId `
            -BridgeUrl $BridgeUrl | Out-Null
    }

    $after = Read-ConfigGuard
    if ($after.baseUrl -eq $BridgeUrl) {
        return [pscustomobject]@{
            Status = "repaired"
            ProviderIds = $providerIds
            Error = ""
        }
    }

    return [pscustomobject]@{
        Status = "repair-stale-or-rewritten"
        ProviderIds = $providerIds
        Error = "Live config no longer points at the bridge after repair"
    }
}

function Invoke-HistoryAutomation {
    if ($SkipHistoryMigration -or $HistoryScope -eq "none") {
        return [pscustomobject]@{
            Status = "disabled"
            Result = $null
            Error = ""
        }
    }
    if ($HistoryScope -eq "conversation" -and -not $ConversationId) {
        return [pscustomobject]@{
            Status = "skipped-missing-conversation"
            Result = $null
            Error = ""
        }
    }

    $python = Get-PythonExecutable
    $arguments = @(
        $AutomationScript,
        "--codex-home",
        $CodexHome,
        "--config",
        $ConfigPath,
        "--scope",
        $HistoryScope,
        "--target-provider",
        $TargetProvider,
        "--idle-seconds",
        [string]$IdleSeconds,
        "--max-items",
        [string]$MaxMigrationsPerRun
    )
    if (-not $IncludeCurrentConversation -and (Test-Path -LiteralPath $RuntimeStatusFile -PathType Leaf)) {
        $runtimeStatus = Get-Content -Raw -LiteralPath $RuntimeStatusFile | ConvertFrom-Json
        if ($runtimeStatus.lastRequest -and $runtimeStatus.lastRequest.conversationId) {
            $arguments += @(
                "--exclude-conversation-id",
                [string]$runtimeStatus.lastRequest.conversationId
            )
        }
    }
    if ($HistoryScope -eq "conversation") {
        $arguments += @("--conversation-id", $ConversationId)
    }
    if ($Apply) {
        $arguments += "--apply"
    }

    $output = & $python @arguments
    if ($LASTEXITCODE -ne 0) {
        return [pscustomobject]@{
            Status = "failed"
            Result = $null
            Error = ($output -join "`n")
        }
    }

    try {
        $result = $output | ConvertFrom-Json
    } catch {
        return [pscustomobject]@{
            Status = "failed-invalid-json"
            Result = $null
            Error = ($output -join "`n")
        }
    }

    $hasError = $result.PSObject.Properties["error"] -and $result.error
    $failureCount = if ($result.PSObject.Properties["failureCount"]) {
        [int]$result.failureCount
    } else {
        0
    }
    $appliedCount = if ($result.PSObject.Properties["appliedCount"]) {
        [int]$result.appliedCount
    } else {
        0
    }
    $status = if (-not $Apply) {
        "planned"
    } elseif ($hasError) {
        "failed"
    } elseif ($failureCount -gt 0) {
        "completed-with-failures"
    } elseif ($appliedCount -gt 0) {
        "applied"
    } else {
        "no-op"
    }
    return [pscustomobject]@{
        Status = $status
        Result = $result
        Error = ""
    }
}

function Invoke-PostSwitchProbe {
    $python = Get-PythonExecutable
    $arguments = @(
        $ProbeScript,
        "--config",
        $ConfigPath,
        "--codex-home",
        $CodexHome,
        "--mode",
        $PostSwitchProbeMode,
        "--scope",
        $PostSwitchScope,
        "--timeout-seconds",
        [string]$ProbeTimeoutSeconds,
        "--state-file",
        $ProbeStateFile,
        "--status-file",
        $ProbeStatusFile
    )
    if ($Apply) {
        $arguments += "--apply"
    }

    $output = & $python @arguments
    if ($LASTEXITCODE -ne 0) {
        return [pscustomobject]@{
            Status = "failed"
            Result = $null
            Error = ($output -join "`n")
        }
    }
    try {
        $result = $output | ConvertFrom-Json
    } catch {
        return [pscustomobject]@{
            Status = "failed-invalid-json"
            Result = $null
            Error = ($output -join "`n")
        }
    }
    return [pscustomobject]@{
        Status = [string]$result.status
        Result = $result
        Error = ""
    }
}

function Update-PostSwitchPolicy {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param([Parameter(Mandatory)][object]$Probe)

    if ($PostSwitchScope -eq "preserve") {
        return [pscustomobject]@{
            Status = "preserved"
            BackupFile = ""
            Scope = ""
            Error = ""
        }
    }
    if (
        $Probe.Result -and
        $Probe.Result.PSObject.Properties["probe"] -and
        $Probe.Result.probe -and
        $Probe.Result.probe.PSObject.Properties["ok"] -and
        $Probe.Result.probe.ok -eq $false
    ) {
        return [pscustomobject]@{
            Status = "skipped-probe-failed"
            BackupFile = ""
            Scope = ""
            Error = ""
        }
    }
    if (-not $Apply) {
        return [pscustomobject]@{
            Status = "planned"
            BackupFile = ""
            Scope = $PostSwitchScope
            Error = ""
        }
    }
    if (-not $PSCmdlet.ShouldProcess($PolicyFile, "Set post-switch bridge scope")) {
        return [pscustomobject]@{
            Status = "planned"
            BackupFile = ""
            Scope = $PostSwitchScope
            Error = ""
        }
    }
    if (-not (Test-Path -LiteralPath $PolicyFile -PathType Leaf)) {
        return [pscustomobject]@{
            Status = "skipped-policy-missing"
            BackupFile = ""
            Scope = ""
            Error = ""
        }
    }

    $backupRoot = Join-Path (Split-Path $StatusFile -Parent) "policy-backups"
    New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
    $backupStamp = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssfffZ")
    $backupSuffix = [guid]::NewGuid().ToString("N").Substring(0, 8)
    $backupFile = Join-Path $backupRoot "policy-$backupStamp-$backupSuffix.json"
    Copy-Item -LiteralPath $PolicyFile -Destination $backupFile
    $policy = Get-Content -Raw -LiteralPath $PolicyFile | ConvertFrom-Json
    $policy.scope = $PostSwitchScope
    $policy.armed = $PostSwitchScope -eq "next"
    $policy.targetConversationId = ""
    if ($PostSwitchScope -eq "all") {
        $policy.conversationIds = @()
    }
    $policy.updatedAt = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $temporary = "$PolicyFile.$PID.tmp"
    $policy | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $temporary -Encoding utf8
    Move-Item -LiteralPath $temporary -Destination $PolicyFile -Force
    return [pscustomobject]@{
        Status = "applied"
        BackupFile = $backupFile
        Scope = $PostSwitchScope
        Error = ""
    }
}

$lockStream = $null
try {
    try {
        $lockStream = [System.IO.File]::Open(
            $LockPath,
            [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None
        )
    } catch {
        Write-Output "status=locked"
        exit 0
    }

    $startedAt = [DateTime]::UtcNow
    $guardBefore = Read-ConfigGuard
    $configRepair = Invoke-ConfigRepair -Guard $guardBefore
    $postSwitchProbe = Invoke-PostSwitchProbe
    $postSwitchPolicy = Update-PostSwitchPolicy -Probe $postSwitchProbe
    $history = Invoke-HistoryAutomation
    $guardAfter = Read-ConfigGuard

    $payload = [pscustomobject]@{
        UpdatedAtUtc = [DateTime]::UtcNow.ToString("o")
        Apply = [bool]$Apply
        StatusFile = $StatusFile
        ConfigRepair = $configRepair
        PostSwitchProbe = $postSwitchProbe
        PostSwitchPolicy = $postSwitchPolicy
        History = $history
        ConfigBefore = $guardBefore
        ConfigAfter = $guardAfter
        DurationSeconds = [math]::Round(
            ([DateTime]::UtcNow - $startedAt).TotalSeconds,
            3
        )
    }
    Write-AutomationStatus -Payload $payload

    Write-Output "status=completed"
    Write-Output "apply=$([bool]$Apply)"
    Write-Output "config_repair_status=$($configRepair.Status)"
    Write-Output "config_route_after=$($guardAfter.baseUrl)"
    Write-Output "post_switch_probe_status=$($postSwitchProbe.Status)"
    $probeDetail = $null
    if ($postSwitchProbe.Result -and $postSwitchProbe.Result.PSObject.Properties["probe"]) {
        $probeDetail = $postSwitchProbe.Result.probe
    }
    if ($probeDetail) {
        $probeOk = if ($probeDetail.PSObject.Properties["ok"]) { $probeDetail.ok } else { "" }
        $probeError = if ($probeDetail.PSObject.Properties["errorCategory"]) {
            $probeDetail.errorCategory
        } else {
            ""
        }
        Write-Output "post_switch_probe_ok=$probeOk"
        Write-Output "post_switch_probe_error_category=$probeError"
    }
    Write-Output "post_switch_policy_status=$($postSwitchPolicy.Status)"
    Write-Output "post_switch_policy_scope=$($postSwitchPolicy.Scope)"
    Write-Output "history_status=$($history.Status)"
    if ($history.Result) {
        Write-Output "history_candidate_count=$($history.Result.candidateCount)"
        Write-Output "history_selected_count=$($history.Result.selectedCount)"
        Write-Output "history_applied_count=$($history.Result.appliedCount)"
    }
} catch {
    $payload = [pscustomobject]@{
        UpdatedAtUtc = [DateTime]::UtcNow.ToString("o")
        Apply = [bool]$Apply
        StatusFile = $StatusFile
        Error = $_.Exception.Message
    }
    Write-AutomationStatus -Payload $payload
    Write-Output "status=failed"
    Write-Output "error=$($_.Exception.Message)"
    exit 1
} finally {
    if ($lockStream) {
        $lockStream.Dispose()
    }
}
