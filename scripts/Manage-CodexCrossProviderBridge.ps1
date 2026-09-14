[CmdletBinding()]
param(
    [ValidateSet(
        "install",
        "uninstall",
        "start",
        "stop",
        "repair",
        "backup",
        "restore",
        "migrate-history",
        "list-snapshots",
        "status"
    )]
    [string]$Action = "status",
    [int]$BridgePort = 15722,
    [string]$UpstreamUrl = "http://127.0.0.1:15721",
    [ValidateSet("all", "next", "conversation")]
    [string]$RuntimeScope = "all",
    [ValidateSet("all", "conversation", "none")]
    [string]$HistoryScope = "all",
    [string]$ConversationId = "",
    [string]$TargetProvider = "custom",
    [switch]$ApplyMigration,
    [switch]$LockNextConversation,
    [string]$SnapshotId = "",
    [switch]$RestoreCcSwitchSettings
)

. (Join-Path $PSScriptRoot "CodexCrossProviderBridge.Common.ps1")

$TaskName = $script:CodexBridgeTaskName
$BridgeScript = Join-Path (Split-Path $PSScriptRoot -Parent) "src\codex_cross_provider_bridge.py"
$RepairScript = Join-Path $PSScriptRoot "Repair-Codex-CCSwitchProviderAlias.ps1"
$AuditScript = Join-Path (Split-Path $PSScriptRoot -Parent) "src\codex_history_audit.py"
$MigrateScript = Join-Path (Split-Path $PSScriptRoot -Parent) "src\codex_thread_provider_migrate.py"
$ConfigPath = Join-Path $env:USERPROFILE ".codex\config.toml"
$BridgeUrl = "http://127.0.0.1:$BridgePort/v1"
$BridgeStateDirectory = Join-Path (Split-Path $PSScriptRoot -Parent) "state"
$PolicyFile = Join-Path $BridgeStateDirectory "policy.json"
$StatusFile = Join-Path $BridgeStateDirectory "status.json"

if (-not (Test-Path -LiteralPath $BridgeScript -PathType Leaf)) {
    throw "Bridge script not found: $BridgeScript"
}

function Get-PythonPath {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) {
        throw "Python 3.10+ is required but python was not found on PATH"
    }
    return $python.Source
}

function Test-BridgePort {
    try {
        $client = [System.Net.Sockets.TcpClient]::new()
        $client.Connect("127.0.0.1", $BridgePort)
        $client.Close()
        return $true
    } catch {
        return $false
    }
}

function Write-RuntimePolicy {
    param(
        [Parameter(Mandatory)][string]$Scope,
        [string]$Conversation,
        [Parameter(Mandatory)][string]$History,
        [switch]$LockNext
    )

    New-Item -ItemType Directory -Path $BridgeStateDirectory -Force | Out-Null
    $conversationIds = @()
    if ($Conversation) {
        $conversationIds = @($Conversation)
    }
    $armed = $Scope -in @("next", "conversation")
    $targetConversationId = ""
    if ($Scope -eq "conversation" -and $Conversation -and -not $LockNext) {
        $targetConversationId = $Conversation
        $armed = $false
    }

    $policy = [pscustomobject]@{
        scope = $Scope
        historyScope = $History
        conversationIds = $conversationIds
        armed = $armed
        targetConversationId = $targetConversationId
        updatedAt = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    }
    $temporary = "$PolicyFile.$PID.tmp"
    $policy | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $temporary -Encoding utf8
    Move-Item -LiteralPath $temporary -Destination $PolicyFile -Force
}

function Invoke-HistoryAudit {
    param([ValidateSet("all", "conversation")][string]$Scope)

    $python = Get-PythonPath
    $arguments = @(
        $AuditScript,
        "--config",
        $ConfigPath,
        "--scope",
        $Scope
    )
    if ($Scope -eq "conversation") {
        if (-not $ConversationId) {
            throw "ConversationId is required when HistoryScope is conversation"
        }
        $arguments += @("--conversation-id", $ConversationId)
    }

    $output = & $python @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "History audit failed"
    }
    return $output | ConvertFrom-Json
}

function Register-BridgeTask {
    param(
        [Parameter(Mandatory)][int]$Port,
        [Parameter(Mandatory)][string]$Upstream
    )

    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 500
    }
    $python = Get-PythonPath
    $arguments = ('"{0}" --listen 127.0.0.1:{1} --upstream {2}' -f (
        $BridgeScript,
        $Port,
        $Upstream
    )) + (' --policy-file "{0}" --status-file "{1}"' -f (
        $PolicyFile,
        $StatusFile
    )) + (' --codex-home "{0}"' -f (Join-Path $env:USERPROFILE ".codex"))
    $taskAction = New-ScheduledTaskAction `
        -Execute $python `
        -Argument $arguments `
        -WorkingDirectory $PSScriptRoot
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -MultipleInstances IgnoreNew
    $principal = New-ScheduledTaskPrincipal `
        -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType Interactive `
        -RunLevel Limited

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $taskAction `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Force | Out-Null
    Start-ScheduledTask -TaskName $TaskName
}

function Unregister-BridgeTask {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }
}

function Repair-HistoricalProviderAlias {
    if ($HistoryScope -eq "none") {
        Write-Output "history_repair=skipped"
        return
    }

    if ($HistoryScope -eq "all") {
        & $RepairScript -ConfigPath $ConfigPath -BridgeUrl $BridgeUrl
        & $RepairScript `
            -ConfigPath $ConfigPath `
            -LegacyProviderId "custom" `
            -BridgeUrl $BridgeUrl

        $config = Get-Content -Raw -LiteralPath $ConfigPath
        foreach ($providerId in @("custom", "cc-switch-official")) {
            $escaped = [regex]::Escape($providerId)
            if ($config -notmatch "(?m)^\[model_providers\.$escaped\]") {
                throw "Historical compatibility provider is missing after repair: $providerId"
            }
        }
        return
    }

    $audit = Invoke-HistoryAudit -Scope "conversation"
    if (-not $audit.threadFound) {
        throw "Conversation was not found in the Codex history index: $ConversationId"
    }
    foreach ($providerId in $audit.missingProviderIds) {
        & $RepairScript `
            -ConfigPath $ConfigPath `
            -LegacyProviderId $providerId `
            -BridgeUrl $BridgeUrl
    }
}

switch ($Action) {
    "install" {
        $baseline = Get-CodexBridgeSnapshot `
            -Reason "pre-install" `
            -ConfigPath $ConfigPath
        if (-not $baseline) {
            $baseline = New-CodexBridgeSnapshot `
                -Reason "pre-install" `
                -ConfigPath $ConfigPath `
                -BridgeScript $BridgeScript `
                -TaskName $TaskName
        }

        try {
            Write-RuntimePolicy `
                -Scope $RuntimeScope `
                -Conversation $ConversationId `
                -History $HistoryScope `
                -LockNext:$LockNextConversation
            Register-BridgeTask -Port $BridgePort -Upstream $UpstreamUrl
            Repair-HistoricalProviderAlias
            if (-not (Test-BridgePort)) {
                throw "Bridge did not start on port $BridgePort"
            }
            Write-Output "status=installed"
            Write-Output "task=$TaskName"
            Write-Output "bridge_url=$BridgeUrl"
            Write-Output "runtime_scope=$RuntimeScope"
            Write-Output "history_scope=$HistoryScope"
            Write-Output "baseline_snapshot=$($baseline.SnapshotId)"
        } catch {
            Restore-CodexBridgeSnapshot -SnapshotDirectory $baseline.Directory | Out-Null
            throw
        }
    }
    "uninstall" {
        $baseline = Get-CodexBridgeSnapshot `
            -Reason "pre-install" `
            -ConfigPath $ConfigPath
        if (-not $baseline) {
            throw "No pre-install snapshot exists for $ConfigPath"
        }
        $result = Restore-CodexBridgeSnapshot `
            -SnapshotDirectory $baseline.Directory `
            -RestoreCcSwitchSettings:$RestoreCcSwitchSettings
        Write-Output "status=restored"
        Write-Output "restored_snapshot=$($result.RestoredSnapshotId)"
        Write-Output "pre_restore_snapshot=$($result.PreRestoreSnapshotId)"
    }
    "start" {
        Start-ScheduledTask -TaskName $TaskName
        Write-Output "status=start_requested"
        Write-Output "task=$TaskName"
    }
    "stop" {
        Stop-ScheduledTask -TaskName $TaskName
        Write-Output "status=stop_requested"
        Write-Output "task=$TaskName"
    }
    "repair" {
        Write-RuntimePolicy `
            -Scope $RuntimeScope `
            -Conversation $ConversationId `
            -History $HistoryScope `
            -LockNext:$LockNextConversation
        Repair-HistoricalProviderAlias
    }
    "backup" {
        $snapshot = New-CodexBridgeSnapshot `
            -Reason "manual" `
            -ConfigPath $ConfigPath `
            -BridgeScript $BridgeScript `
            -TaskName $TaskName
        Write-Output "status=backed_up"
        Write-Output "snapshot_id=$($snapshot.SnapshotId)"
        Write-Output "snapshot_directory=$($snapshot.Directory)"
    }
    "restore" {
        if ($SnapshotId) {
            $snapshot = Get-CodexBridgeSnapshot -SnapshotId $SnapshotId
        } else {
            $snapshot = Get-CodexBridgeSnapshot `
                -Reason "pre-install" `
                -ConfigPath $ConfigPath
        }
        if (-not $snapshot) {
            throw "Requested snapshot was not found"
        }

        $result = Restore-CodexBridgeSnapshot `
            -SnapshotDirectory $snapshot.Directory `
            -RestoreCcSwitchSettings:$RestoreCcSwitchSettings
        Write-Output "status=restored"
        Write-Output "restored_snapshot=$($result.RestoredSnapshotId)"
        Write-Output "pre_restore_snapshot=$($result.PreRestoreSnapshotId)"
    }
    "migrate-history" {
        if (-not $ConversationId) {
            throw "ConversationId is required for migrate-history"
        }
        if (-not (Test-Path -LiteralPath $MigrateScript -PathType Leaf)) {
            throw "Migration script not found: $MigrateScript"
        }
        if ($ApplyMigration) {
            $snapshot = New-CodexBridgeSnapshot `
                -Reason "pre-migration" `
                -ConfigPath $ConfigPath `
                -BridgeScript $BridgeScript `
                -TaskName $TaskName
        }

        $python = Get-PythonPath
        $arguments = @(
            $MigrateScript,
            "--conversation-id",
            $ConversationId,
            "--target-provider",
            $TargetProvider
        )
        if ($ApplyMigration) {
            $arguments += "--apply"
        }
        $output = & $python @arguments
        if ($LASTEXITCODE -ne 0) {
            throw "Thread provider migration failed"
        }
        Write-Output $output
        if ($ApplyMigration) {
            Write-Output "config_snapshot=$($snapshot.SnapshotId)"
        }
    }
    "list-snapshots" {
        Get-CodexBridgeSnapshotList |
            Where-Object ConfigPath -eq (Get-NormalizedPath -Path $ConfigPath) |
            Select-Object SnapshotId,CreatedAtUtc,Reason,TaskPresent,Directory |
            Format-Table -AutoSize
    }
    "status" {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        $config = if (Test-Path -LiteralPath $ConfigPath -PathType Leaf) {
            Get-Content -Raw -LiteralPath $ConfigPath
        } else {
            ""
        }
        $baseline = Get-CodexBridgeSnapshot `
            -Reason "pre-install" `
            -ConfigPath $ConfigPath
        $snapshotCount = @(
            Get-CodexBridgeSnapshotList |
                Where-Object ConfigPath -eq (Get-NormalizedPath -Path $ConfigPath)
        ).Count
        $policy = if (Test-Path -LiteralPath $PolicyFile -PathType Leaf) {
            Get-Content -Raw -LiteralPath $PolicyFile | ConvertFrom-Json
        } else {
            $null
        }
        $runtimeStatus = if (Test-Path -LiteralPath $StatusFile -PathType Leaf) {
            Get-Content -Raw -LiteralPath $StatusFile | ConvertFrom-Json
        } else {
            $null
        }

        Write-Output "task_present=$([bool]$task)"
        Write-Output "task_state=$($task.State)"
        Write-Output "bridge_listening=$(Test-BridgePort)"
        Write-Output "bridge_url=$BridgeUrl"
        Write-Output "config_uses_bridge=$($config.Contains($BridgeUrl))"
        Write-Output "custom_provider_present=$($config -match '(?m)^\[model_providers\.custom\]')"
        Write-Output "official_alias_present=$($config -match '(?m)^\[model_providers\.cc-switch-official\]')"
        Write-Output "baseline_snapshot=$(if ($baseline) { $baseline.SnapshotId } else { 'none' })"
        Write-Output "snapshot_count=$snapshotCount"
        Write-Output "runtime_scope=$(if ($policy) { $policy.scope } else { 'unknown' })"
        Write-Output "history_scope=$(if ($policy -and $policy.PSObject.Properties['historyScope']) { $policy.historyScope } else { 'unknown' })"
        Write-Output "runtime_armed=$(if ($policy) { $policy.armed } else { 'unknown' })"
        Write-Output "target_conversation_id=$(if ($policy) { $policy.targetConversationId } else { '' })"
        if ($runtimeStatus -and $runtimeStatus.lastRequest) {
            $lastRequest = $runtimeStatus.lastRequest
            $lastConversationId = if ($lastRequest.PSObject.Properties["conversationId"]) { $lastRequest.conversationId } else { "" }
            $lastConversationTitle = if ($lastRequest.PSObject.Properties["conversationTitle"]) { $lastRequest.conversationTitle } else { "" }
            $lastConversationCwd = if ($lastRequest.PSObject.Properties["cwd"]) { $lastRequest.cwd } else { "" }
            $lastConversationProvider = if ($lastRequest.PSObject.Properties["modelProvider"]) { $lastRequest.modelProvider } else { "" }
            $lastNeedsRepair = if ($lastRequest.PSObject.Properties["needsRepair"]) { $lastRequest.needsRepair } else { "unknown" }
            $lastRepairStatus = if ($lastRequest.PSObject.Properties["repairStatus"]) { $lastRequest.repairStatus } else { "unknown" }
            $lastUpstreamStatus = if ($lastRequest.PSObject.Properties["upstreamStatus"]) { $lastRequest.upstreamStatus } else { "" }
            Write-Output "last_conversation_id=$lastConversationId"
            Write-Output "last_conversation_title=$lastConversationTitle"
            Write-Output "last_conversation_cwd=$lastConversationCwd"
            Write-Output "last_conversation_provider=$lastConversationProvider"
            Write-Output "last_needs_repair=$lastNeedsRepair"
            Write-Output "last_repair_status=$lastRepairStatus"
            Write-Output "last_upstream_status=$lastUpstreamStatus"
        } else {
            Write-Output "last_repair_status=no-request-recorded"
        }
        $auditConversationId = if ($ConversationId) {
            $ConversationId
        } elseif ($runtimeStatus -and $runtimeStatus.lastRequest -and $lastConversationId) {
            $lastConversationId
        } else {
            ""
        }
        if ($auditConversationId) {
            $savedConversationId = $ConversationId
            $ConversationId = $auditConversationId
            $audit = Invoke-HistoryAudit -Scope "conversation"
            $ConversationId = $savedConversationId
            Write-Output "conversation_history_found=$($audit.threadFound)"
            Write-Output "conversation_history_repair_required=$($audit.historyRepairRequired)"
            Write-Output "conversation_missing_provider_ids=$($audit.missingProviderIds -join ',')"
            Write-Output "conversation_runtime_provider_ids=$($audit.runtimeProviderIds -join ',')"
            Write-Output "remote_compact_risk=$($audit.remoteCompactRisk)"
            if ($audit.threads -and $audit.threads.Count -gt 0) {
                Write-Output "conversation_title=$($audit.threads[0].title)"
                Write-Output "conversation_cwd=$($audit.threads[0].cwd)"
                Write-Output "conversation_provider=$($audit.threads[0].modelProvider)"
            }
            if ($audit.remoteCompactRisk) {
                Write-Output "remote_compact_repair_command=.\scripts\Manage-CodexCrossProviderBridge.ps1 -Action migrate-history -ConversationId `"$auditConversationId`" -TargetProvider custom -ApplyMigration"
            }
        }
    }
}
