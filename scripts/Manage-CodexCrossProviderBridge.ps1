[CmdletBinding()]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute(
    "PSReviewUnusedParameter",
    "RouteRepairMode",
    Justification = "Forwarded from script scope by lifecycle policy helper functions."
)]
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
        "list-migrations",
        "restore-migration",
        "enable-automation",
        "disable-automation",
        "enable-compact-hook",
        "disable-compact-hook",
        "set-lifecycle-policy",
        "list-hook-backups",
        "restore-hook-backup",
        "list-policy-backups",
        "restore-policy-backup",
        "probe-provider",
        "probe-after-switch",
        "branch-conversation",
        "list-branches",
        "list-snapshots",
        "status"
    )]
    [string]$Action = "status",
    [string]$CodexHome = (Join-Path $env:USERPROFILE ".codex"),
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
    [switch]$RestoreCcSwitchSettings,
    [ValidateSet("none", "all", "conversation")]
    [string]$AutoHistoryScope = "all",
    [ValidateRange(0, [int]::MaxValue)]
    [int]$AutoIdleSeconds = 300,
    [ValidateRange(1, [int]::MaxValue)]
    [int]$AutoMaxMigrationsPerRun = 10,
    [switch]$EnableAutomation,
    [switch]$RunAutomationNow,
    [switch]$IncludeCurrentConversation,
    [string]$MigrationBackupDirectory = "",
    [ValidateSet("disabled", "cli", "app-server")]
    [string]$PostSwitchProbeMode = "cli",
    [ValidateSet("preserve", "next", "all")]
    [string]$PostSwitchScope = "next",
    [ValidateRange(1, 300)]
    [int]$ProbeTimeoutSeconds = 30,
    [ValidateSet(
        "disabled",
        "inspect",
        "repair-and-continue",
        "repair-and-stop",
        "repair-and-branch",
        "branch-only",
        "block-only"
    )]
    [string]$CompactHookMode = "repair-and-continue",
    [ValidateSet("app-server", "cli")]
    [string]$BranchBackend = "app-server",
    [ValidateSet("disabled", "repair", "repair-and-probe")]
    [string]$SessionStartMode = "repair",
    [ValidateSet("disabled", "inspect", "repair")]
    [string]$RouteRepairMode = "repair",
    [switch]$AllowAutoBranch,
    [switch]$EnableCompactHook,
    [switch]$ApplyOperation,
    [string]$TargetModel = "",
    [string]$ContinuePrompt = "",
    [string]$HookBackupDirectory = "",
    [string]$PolicyBackupFile = "",
    [string]$ConfigPath = "",
    [string]$BridgeStateDirectory = ""
)

. (Join-Path $PSScriptRoot "CodexCrossProviderBridge.Common.ps1")

$TaskName = $script:CodexBridgeTaskName
$AutomationTaskName = $script:CodexBridgeAutomationTaskName
$BridgeScript = Join-Path (Split-Path $PSScriptRoot -Parent) "src\codex_cross_provider_bridge.py"
$AutomationScript = Join-Path $PSScriptRoot "Invoke-CodexCrossProviderAutomation.ps1"
$LifecycleManager = Join-Path $PSScriptRoot "Manage-CodexLifecycleHooks.ps1"
$ProbeWrapper = Join-Path $PSScriptRoot "Invoke-CodexProviderProbe.ps1"
$BranchWrapper = Join-Path $PSScriptRoot "Invoke-CodexBranchHandoff.ps1"
$RepairScript = Join-Path $PSScriptRoot "Repair-Codex-CCSwitchProviderAlias.ps1"
$AuditScript = Join-Path (Split-Path $PSScriptRoot -Parent) "src\codex_history_audit.py"
$MigrateScript = Join-Path (Split-Path $PSScriptRoot -Parent) "src\codex_thread_provider_migrate.py"
$TemplateGuardScript = Join-Path (Split-Path $PSScriptRoot -Parent) "src\codex_ccswitch_template_guard.py"
$CcSwitchDatabase = Join-Path $env:USERPROFILE ".cc-switch\cc-switch.db"
if (-not $ConfigPath) {
    $ConfigPath = Join-Path $CodexHome "config.toml"
}
$BridgeUrl = "http://127.0.0.1:$BridgePort/v1"
if (-not $BridgeStateDirectory) {
    $BridgeStateDirectory = Join-Path (Split-Path $PSScriptRoot -Parent) "state"
}
$PolicyFile = Join-Path $BridgeStateDirectory "policy.json"
$StatusFile = Join-Path $BridgeStateDirectory "status.json"
$AutomationStatusFile = Join-Path $BridgeStateDirectory "automation-status.json"
$LifecyclePolicyFile = Join-Path $BridgeStateDirectory "lifecycle-policy.json"
$LifecycleStatusFile = Join-Path $BridgeStateDirectory "lifecycle-status.json"
$ProbeStateFile = Join-Path $BridgeStateDirectory "provider-probe-state.json"
$ProbeStatusFile = Join-Path $BridgeStateDirectory "provider-probe-status.json"
$BranchHistoryFile = Join-Path $BridgeStateDirectory "branch-history.jsonl"
$MigrationBackupRoot = Join-Path $CodexHome "backups\codex-thread-provider-migrate"

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
    )) + (' --codex-home "{0}" --cc-switch-db "{1}"' -f (
        $CodexHome,
        $CcSwitchDatabase
    ))
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

function Get-AutomationScriptArgumentString {
    $arguments = @(
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        ("`"{0}`"" -f $AutomationScript),
        "-Apply",
        "-HistoryScope",
        $AutoHistoryScope,
        "-TargetProvider",
        $TargetProvider,
        "-IdleSeconds",
        [string]$AutoIdleSeconds,
        "-MaxMigrationsPerRun",
        [string]$AutoMaxMigrationsPerRun,
        "-PostSwitchProbeMode",
        $PostSwitchProbeMode,
        "-PostSwitchScope",
        $PostSwitchScope,
        "-ProbeTimeoutSeconds",
        [string]$ProbeTimeoutSeconds,
        "-StatusFile",
        ("`"{0}`"" -f $AutomationStatusFile),
        "-ProbeStateFile",
        ("`"{0}`"" -f $ProbeStateFile),
        "-ProbeStatusFile",
        ("`"{0}`"" -f $ProbeStatusFile)
    )
    if ($AutoHistoryScope -eq "conversation" -and $ConversationId) {
        $arguments += @("-ConversationId", ("`"{0}`"" -f $ConversationId))
    }
    if ($IncludeCurrentConversation) {
        $arguments += "-IncludeCurrentConversation"
    }
    return ($arguments -join " ")
}

function Register-AutomationTask {
    if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
        throw "Install the bridge task before enabling automation"
    }
    if ($AutoHistoryScope -eq "conversation" -and -not $ConversationId) {
        throw "ConversationId is required when AutoHistoryScope is conversation"
    }
    if (-not (Test-Path -LiteralPath $AutomationScript -PathType Leaf)) {
        throw "Automation script not found: $AutomationScript"
    }

    Unregister-AutomationTask

    # Prefer PowerShell 7: Windows PowerShell can inherit a PowerShell 7
    # module path where cmdlets used by the automation script are missing.
    $pwsh = Get-Command pwsh.exe -ErrorAction SilentlyContinue
    $powershell = if ($pwsh) {
        $pwsh.Source
    } else {
        (Get-Command powershell.exe -ErrorAction Stop).Source
    }
    $arguments = Get-AutomationScriptArgumentString
    $taskAction = New-ScheduledTaskAction `
        -Execute $powershell `
        -Argument $arguments `
        -WorkingDirectory $PSScriptRoot
    $trigger = New-ScheduledTaskTrigger `
        -Once `
        -At ((Get-Date).AddMinutes(1)) `
        -RepetitionInterval (New-TimeSpan -Minutes 1) `
        -RepetitionDuration (New-TimeSpan -Days 3650)
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -MultipleInstances IgnoreNew
    $principal = New-ScheduledTaskPrincipal `
        -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType Interactive `
        -RunLevel Limited

    Register-ScheduledTask `
        -TaskName $AutomationTaskName `
        -Action $taskAction `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Force | Out-Null
}

function Unregister-AutomationTask {
    if (Get-ScheduledTask -TaskName $AutomationTaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $AutomationTaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $AutomationTaskName -Confirm:$false
    }
}

function Backup-LifecyclePolicy {
    if (-not (Test-Path -LiteralPath $LifecyclePolicyFile -PathType Leaf)) {
        return [pscustomobject]@{
            Existed = $false
            Path = ""
        }
    }
    $backupRoot = Join-Path $BridgeStateDirectory "lifecycle-policy-backups"
    New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
    $stamp = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssfffZ")
    $suffix = [guid]::NewGuid().ToString("N").Substring(0, 8)
    $backupFile = Join-Path $backupRoot "policy-$stamp-$suffix.json"
    Copy-Item -LiteralPath $LifecyclePolicyFile -Destination $backupFile
    return [pscustomobject]@{
        Existed = $true
        Path = $backupFile
    }
}

function Restore-LifecyclePolicyBackup {
    param([Parameter(Mandatory)][object]$Backup)

    if (-not $Backup.Existed) {
        if (Test-Path -LiteralPath $LifecyclePolicyFile -PathType Leaf) {
            Remove-Item -LiteralPath $LifecyclePolicyFile -Force
        }
        return
    }
    Copy-Item -LiteralPath $Backup.Path -Destination $LifecyclePolicyFile -Force
}

function Set-LifecyclePolicy {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param()

    if (-not (Test-Path -LiteralPath $LifecycleManager -PathType Leaf)) {
        throw "Lifecycle manager not found: $LifecycleManager"
    }
    if (-not $PSCmdlet.ShouldProcess($LifecyclePolicyFile, "Set lifecycle policy")) {
        return
    }
    $backup = Backup-LifecyclePolicy
    try {
        & $LifecycleManager `
            -Action set-policy `
            -CodexHome $CodexHome `
            -ConfigPath $ConfigPath `
            -PolicyPath $LifecyclePolicyFile `
            -StatusFile $LifecycleStatusFile `
            -CompactMode $CompactHookMode `
            -AutoBranch ([string]$AllowAutoBranch).ToLowerInvariant() `
            -BranchBackend $BranchBackend `
            -PostSwitchProbeMode $PostSwitchProbeMode `
            -PostSwitchScope $PostSwitchScope `
            -SessionStartMode $SessionStartMode `
            -RouteRepairMode $RouteRepairMode `
            -BridgeUrl $BridgeUrl `
            -ProbeTimeoutSeconds $ProbeTimeoutSeconds `
            -Apply
        if ($LASTEXITCODE -ne 0) {
            throw "Lifecycle policy update failed"
        }
    } catch {
        Restore-LifecyclePolicyBackup -Backup $backup
        throw
    }
    Write-Output "lifecycle_policy_backup=$($backup.Path)"
}

function Install-LifecycleHook {
    if (-not (Test-Path -LiteralPath $LifecycleManager -PathType Leaf)) {
        throw "Lifecycle manager not found: $LifecycleManager"
    }
    $policyBackup = Backup-LifecyclePolicy
    Set-LifecyclePolicy
    try {
        & $LifecycleManager `
            -Action install-hooks `
            -CodexHome $CodexHome `
            -ConfigPath $ConfigPath `
            -PolicyPath $LifecyclePolicyFile `
            -StatusFile $LifecycleStatusFile `
            -BridgeUrl $BridgeUrl `
            -Apply
        if ($LASTEXITCODE -ne 0) {
            throw "Lifecycle hook installation failed"
        }
    } catch {
        Restore-LifecyclePolicyBackup -Backup $policyBackup
        & $LifecycleManager `
            -Action uninstall-hooks `
            -CodexHome $CodexHome `
            -ConfigPath $ConfigPath `
            -PolicyPath $LifecyclePolicyFile `
            -StatusFile $LifecycleStatusFile `
            -BridgeUrl $BridgeUrl `
            -Apply | Out-Null
        throw
    }
    Write-Output "lifecycle_hooks=installed"
    Write-Output "route_repair_mode=$RouteRepairMode"
    Write-Output "hook_trust_required=true"
}

function Unregister-LifecycleHook {
    if (-not (Test-Path -LiteralPath $LifecycleManager -PathType Leaf)) {
        return
    }
    & $LifecycleManager `
        -Action uninstall-hooks `
        -CodexHome $CodexHome `
        -ConfigPath $ConfigPath `
        -PolicyPath $LifecyclePolicyFile `
        -StatusFile $LifecycleStatusFile `
        -Apply | Out-Null
}

function Invoke-AutomationNow {
    if (-not (Test-Path -LiteralPath $AutomationScript -PathType Leaf)) {
        throw "Automation script not found: $AutomationScript"
    }
    & $AutomationScript `
        -Apply `
        -HistoryScope $AutoHistoryScope `
        -ConversationId $ConversationId `
        -TargetProvider $TargetProvider `
        -IdleSeconds $AutoIdleSeconds `
        -MaxMigrationsPerRun $AutoMaxMigrationsPerRun `
        -PostSwitchProbeMode $PostSwitchProbeMode `
        -PostSwitchScope $PostSwitchScope `
        -ProbeTimeoutSeconds $ProbeTimeoutSeconds `
        -StatusFile $AutomationStatusFile `
        -ProbeStateFile $ProbeStateFile `
        -ProbeStatusFile $ProbeStatusFile `
        -IncludeCurrentConversation:$IncludeCurrentConversation
}

function Get-ThreadMigrationBackup {
    if (-not (Test-Path -LiteralPath $MigrationBackupRoot -PathType Container)) {
        return @()
    }
    return Get-ChildItem -LiteralPath $MigrationBackupRoot -Directory |
        Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "manifest.json") } |
        ForEach-Object {
            $manifest = Get-Content -Raw -LiteralPath (Join-Path $_.FullName "manifest.json") |
                ConvertFrom-Json
            [pscustomobject]@{
                ConversationId = $manifest.conversationId
                SourceProvider = $manifest.sourceProvider
                TargetProvider = $manifest.targetProvider
                CreatedAt = if ($manifest.createdAt) {
                    [DateTimeOffset]::FromUnixTimeSeconds([long]$manifest.createdAt).LocalDateTime
                } else {
                    $null
                }
                Directory = $_.FullName
            }
        } |
        Sort-Object CreatedAt -Descending
}

function Restore-ThreadProviderMigration {
    param(
        [Parameter(Mandatory)][string]$BackupDirectory,
        [switch]$Apply
    )

    $manifestPath = Join-Path $BackupDirectory "manifest.json"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "Migration manifest not found: $manifestPath"
    }
    $manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
    if (-not $manifest.conversationId -or -not $manifest.sourceProvider) {
        throw "Migration manifest does not contain the required original provider metadata"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $BackupDirectory "rollout.jsonl") -PathType Leaf)) {
        throw "Migration rollout backup not found in $BackupDirectory"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $BackupDirectory "state_5.sqlite") -PathType Leaf)) {
        throw "Migration SQLite backup not found in $BackupDirectory"
    }

    $python = Get-PythonPath
    $planOutput = & $python $MigrateScript `
        --conversation-id $manifest.conversationId `
        --target-provider $manifest.sourceProvider
    if ($LASTEXITCODE -ne 0) {
        throw "Could not inspect the migration target conversation"
    }
    $plan = $planOutput | ConvertFrom-Json
    if ($plan.currentProvider -ne $manifest.targetProvider) {
        throw (
            "Conversation provider is {$($plan.currentProvider)}; expected " +
            "{$($manifest.targetProvider)} from the migration manifest"
        )
    }

    if (-not $Apply) {
        Write-Output "status=restore_planned"
        Write-Output "conversation_id=$($manifest.conversationId)"
        Write-Output "restore_to_provider=$($manifest.sourceProvider)"
        Write-Output "current_provider=$($plan.currentProvider)"
        return
    }

    $output = & $python $MigrateScript `
        --conversation-id $manifest.conversationId `
        --target-provider $manifest.sourceProvider `
        --apply
    if ($LASTEXITCODE -ne 0) {
        throw "Migration restore failed"
    }
    Write-Output "status=restored"
    Write-Output "conversation_id=$($manifest.conversationId)"
    Write-Output "restored_provider=$($manifest.sourceProvider)"
    Write-Output $output
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
        if (Test-Path -LiteralPath $CcSwitchDatabase -PathType Leaf) {
            $python = Get-PythonPath
            & $python $TemplateGuardScript `
                --database $CcSwitchDatabase `
                --bridge-url $BridgeUrl `
                --apply
            if ($LASTEXITCODE -ne 0) {
                throw "CC Switch provider template guard failed"
            }
        } else {
            Write-Output "cc_switch_template_guard=skipped_database_missing"
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
    if (Test-Path -LiteralPath $CcSwitchDatabase -PathType Leaf) {
        $python = Get-PythonPath
        & $python $TemplateGuardScript `
            --database $CcSwitchDatabase `
            --bridge-url $BridgeUrl `
            --apply
        if ($LASTEXITCODE -ne 0) {
            throw "CC Switch provider template guard failed"
        }
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
            if ($EnableAutomation) {
                Register-AutomationTask
                if ($RunAutomationNow) {
                    Invoke-AutomationNow
                }
            }
            if ($EnableCompactHook) {
                Install-LifecycleHook
            }
            Write-Output "status=installed"
            Write-Output "task=$TaskName"
            Write-Output "bridge_url=$BridgeUrl"
            Write-Output "runtime_scope=$RuntimeScope"
            Write-Output "history_scope=$HistoryScope"
            Write-Output "automation_enabled=$([bool]$EnableAutomation)"
            Write-Output "compact_hook_enabled=$([bool]$EnableCompactHook)"
            Write-Output "baseline_snapshot=$($baseline.SnapshotId)"
        } catch {
            Unregister-AutomationTask
            Unregister-LifecycleHook
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
        Unregister-AutomationTask
        Unregister-LifecycleHook
        $result = Restore-CodexBridgeSnapshot `
            -SnapshotDirectory $baseline.Directory `
            -RestoreCcSwitchSettings:$RestoreCcSwitchSettings
        Write-Output "status=restored"
        Write-Output "restored_snapshot=$($result.RestoredSnapshotId)"
        Write-Output "pre_restore_snapshot=$($result.PreRestoreSnapshotId)"
    }
    "start" {
        Start-ScheduledTask -TaskName $TaskName
        if (Get-ScheduledTask -TaskName $AutomationTaskName -ErrorAction SilentlyContinue) {
            Start-ScheduledTask -TaskName $AutomationTaskName
        }
        Write-Output "status=start_requested"
        Write-Output "task=$TaskName"
    }
    "stop" {
        if (Get-ScheduledTask -TaskName $AutomationTaskName -ErrorAction SilentlyContinue) {
            Stop-ScheduledTask -TaskName $AutomationTaskName
        }
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

        Unregister-AutomationTask
        Unregister-LifecycleHook
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
    "list-migrations" {
        Get-ThreadMigrationBackup |
            Select-Object ConversationId,SourceProvider,TargetProvider,CreatedAt,Directory |
            Format-Table -AutoSize
    }
    "restore-migration" {
        if (-not $MigrationBackupDirectory) {
            throw "MigrationBackupDirectory is required for restore-migration"
        }
        $resolvedBackup = [System.IO.Path]::GetFullPath($MigrationBackupDirectory)
        if (-not (Test-Path -LiteralPath $resolvedBackup -PathType Container)) {
            throw "Migration backup directory not found: $resolvedBackup"
        }
        Restore-ThreadProviderMigration `
            -BackupDirectory $resolvedBackup `
            -Apply:$ApplyMigration
    }
    "enable-automation" {
        Register-AutomationTask
        if ($RunAutomationNow) {
            Invoke-AutomationNow
        }
        Write-Output "status=automation_enabled"
        Write-Output "automation_task=$AutomationTaskName"
        Write-Output "auto_history_scope=$AutoHistoryScope"
        Write-Output "auto_idle_seconds=$AutoIdleSeconds"
        Write-Output "auto_max_migrations_per_run=$AutoMaxMigrationsPerRun"
        Write-Output "auto_include_current_conversation=$([bool]$IncludeCurrentConversation)"
    }
    "disable-automation" {
        Unregister-AutomationTask
        Write-Output "status=automation_disabled"
        Write-Output "automation_task=$AutomationTaskName"
    }
    "enable-compact-hook" {
        if (-not $ApplyOperation) {
            throw "ApplyOperation is required for enable-compact-hook"
        }
        Install-LifecycleHook
        Write-Output "status=compact_hook_enabled"
        Write-Output "policy_file=$LifecyclePolicyFile"
        Write-Output "compact_mode=$CompactHookMode"
        Write-Output "auto_branch=$([bool]$AllowAutoBranch)"
        Write-Output "session_start_mode=$SessionStartMode"
    }
    "disable-compact-hook" {
        Unregister-LifecycleHook
        Write-Output "status=compact_hook_disabled"
    }
    "set-lifecycle-policy" {
        if (-not $ApplyOperation) {
            throw "ApplyOperation is required for set-lifecycle-policy"
        }
        Set-LifecyclePolicy
        Write-Output "status=lifecycle_policy_updated"
        Write-Output "compact_mode=$CompactHookMode"
        Write-Output "auto_branch=$([bool]$AllowAutoBranch)"
        Write-Output "session_start_mode=$SessionStartMode"
        Write-Output "branch_backend=$BranchBackend"
        Write-Output "post_switch_probe_mode=$PostSwitchProbeMode"
        Write-Output "post_switch_scope=$PostSwitchScope"
    }
    "list-hook-backups" {
        & $LifecycleManager `
            -Action list-backups `
            -CodexHome $CodexHome `
            -ConfigPath $ConfigPath `
            -PolicyPath $LifecyclePolicyFile `
            -StatusFile $LifecycleStatusFile
    }
    "restore-hook-backup" {
        if (-not $ApplyOperation) {
            throw "ApplyOperation is required for restore-hook-backup"
        }
        if (-not $HookBackupDirectory) {
            throw "HookBackupDirectory is required for restore-hook-backup"
        }
        & $LifecycleManager `
            -Action restore-hooks `
            -CodexHome $CodexHome `
            -ConfigPath $ConfigPath `
            -PolicyPath $LifecyclePolicyFile `
            -StatusFile $LifecycleStatusFile `
            -BackupDirectory $HookBackupDirectory `
            -Apply
    }
    "list-policy-backups" {
        & $LifecycleManager `
            -Action list-policy-backups `
            -CodexHome $CodexHome `
            -ConfigPath $ConfigPath `
            -PolicyPath $LifecyclePolicyFile `
            -StatusFile $LifecycleStatusFile
    }
    "restore-policy-backup" {
        if (-not $ApplyOperation) {
            throw "ApplyOperation is required for restore-policy-backup"
        }
        if (-not $PolicyBackupFile) {
            throw "PolicyBackupFile is required for restore-policy-backup"
        }
        & $LifecycleManager `
            -Action restore-policy-backup `
            -CodexHome $CodexHome `
            -ConfigPath $ConfigPath `
            -PolicyPath $LifecyclePolicyFile `
            -StatusFile $LifecycleStatusFile `
            -BackupFile $PolicyBackupFile `
            -Apply
    }
    "probe-provider" {
        if (-not $ApplyOperation) {
            throw "ApplyOperation is required for probe-provider"
        }
        & $ProbeWrapper `
            -Mode $PostSwitchProbeMode `
            -Scope $PostSwitchScope `
            -TimeoutSeconds $ProbeTimeoutSeconds `
            -StateFile $ProbeStateFile `
            -StatusFile $ProbeStatusFile `
            -Apply
    }
    "probe-after-switch" {
        if (-not $ApplyOperation) {
            throw "ApplyOperation is required for probe-after-switch"
        }
        & $ProbeWrapper `
            -Mode $PostSwitchProbeMode `
            -Scope $PostSwitchScope `
            -TimeoutSeconds $ProbeTimeoutSeconds `
            -StateFile $ProbeStateFile `
            -StatusFile $ProbeStatusFile `
            -Apply
    }
    "branch-conversation" {
        if (-not $ApplyOperation) {
            throw "ApplyOperation is required for branch-conversation"
        }
        if (-not $ConversationId) {
            throw "ConversationId is required for branch-conversation"
        }
        & $BranchWrapper `
            -ConversationId $ConversationId `
            -TargetProvider $TargetProvider `
            -TargetModel $TargetModel `
            -Backend $BranchBackend `
            -ContinuePrompt $ContinuePrompt `
            -HistoryFile $BranchHistoryFile `
            -Apply
    }
    "list-branches" {
        & $BranchWrapper -HistoryFile $BranchHistoryFile -List
    }
    "list-snapshots" {
        Get-CodexBridgeSnapshotList |
            Where-Object ConfigPath -eq (Get-NormalizedPath -Path $ConfigPath) |
            Select-Object SnapshotId,CreatedAtUtc,Reason,TaskPresent,Directory |
            Format-Table -AutoSize
    }
    "status" {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        $automationTask = Get-ScheduledTask `
            -TaskName $AutomationTaskName `
            -ErrorAction SilentlyContinue
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
        $automationStatus = if (Test-Path -LiteralPath $AutomationStatusFile -PathType Leaf) {
            Get-Content -Raw -LiteralPath $AutomationStatusFile | ConvertFrom-Json
        } else {
            $null
        }
        $lifecycleStatus = if (Test-Path -LiteralPath $LifecycleStatusFile -PathType Leaf) {
            Get-Content -Raw -LiteralPath $LifecycleStatusFile | ConvertFrom-Json
        } else {
            $null
        }
        $lifecycle = $null
        if (Test-Path -LiteralPath $LifecycleManager -PathType Leaf) {
            $lifecycleOutput = & $LifecycleManager `
                -Action status `
                -CodexHome $CodexHome `
                -ConfigPath $ConfigPath `
                -PolicyPath $LifecyclePolicyFile `
                -StatusFile $LifecycleStatusFile
            if ($LASTEXITCODE -eq 0) {
                $lifecycle = $lifecycleOutput | ConvertFrom-Json
            }
        }

        Write-Output "task_present=$([bool]$task)"
        Write-Output "task_state=$(if ($task) { $task.State } else { '' })"
        Write-Output "automation_task_present=$([bool]$automationTask)"
        Write-Output "automation_task_state=$(if ($automationTask) { $automationTask.State } else { '' })"
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
        Write-Output "automation_status_updated_at=$(if ($automationStatus) { $automationStatus.UpdatedAtUtc } else { '' })"
        Write-Output "automation_config_repair_status=$(if ($automationStatus -and $automationStatus.ConfigRepair) { $automationStatus.ConfigRepair.Status } else { 'unknown' })"
        Write-Output "automation_history_status=$(if ($automationStatus -and $automationStatus.History) { $automationStatus.History.Status } else { 'unknown' })"
        Write-Output "automation_history_candidate_count=$(if ($automationStatus -and $automationStatus.History -and $automationStatus.History.Result) { $automationStatus.History.Result.candidateCount } else { '' })"
        Write-Output "automation_history_applied_count=$(if ($automationStatus -and $automationStatus.History -and $automationStatus.History.Result) { $automationStatus.History.Result.appliedCount } else { '' })"
        Write-Output "automation_post_switch_probe_status=$(if ($automationStatus -and $automationStatus.PostSwitchProbe) { $automationStatus.PostSwitchProbe.Status } else { 'unknown' })"
        $automationProbeDetail = $null
        if ($automationStatus -and $automationStatus.PostSwitchProbe -and $automationStatus.PostSwitchProbe.Result -and $automationStatus.PostSwitchProbe.Result.PSObject.Properties["probe"]) {
            $automationProbeDetail = $automationStatus.PostSwitchProbe.Result.probe
        }
        Write-Output "automation_post_switch_probe_ok=$(if ($automationProbeDetail -and $automationProbeDetail.PSObject.Properties['ok']) { $automationProbeDetail.ok } else { '' })"
        Write-Output "automation_post_switch_probe_error_category=$(if ($automationProbeDetail -and $automationProbeDetail.PSObject.Properties['errorCategory']) { $automationProbeDetail.errorCategory } else { '' })"
        Write-Output "automation_post_switch_policy_status=$(if ($automationStatus -and $automationStatus.PostSwitchPolicy) { $automationStatus.PostSwitchPolicy.Status } else { 'unknown' })"
        Write-Output "automation_post_switch_policy_scope=$(if ($automationStatus -and $automationStatus.PostSwitchPolicy) { $automationStatus.PostSwitchPolicy.Scope } else { '' })"
        Write-Output "lifecycle_hooks_installed=$(if ($lifecycle) { $lifecycle.hooks.managedHooksInstalled } else { 'unknown' })"
        Write-Output "lifecycle_hook_backup_count=$(if ($lifecycle) { $lifecycle.hooks.backupCount } else { '' })"
        Write-Output "compact_repair_mode=$(if ($lifecycle) { $lifecycle.policy.compactRepairMode } else { 'unknown' })"
        Write-Output "auto_branch_enabled=$(if ($lifecycle) { $lifecycle.policy.autoBranchEnabled } else { 'unknown' })"
        Write-Output "session_start_mode=$(if ($lifecycle) { $lifecycle.policy.sessionStartMode } else { 'unknown' })"
        Write-Output "route_repair_mode=$(if ($lifecycle -and $lifecycle.policy.PSObject.Properties['routeRepairMode']) { $lifecycle.policy.routeRepairMode } else { 'unknown' })"
        Write-Output "post_switch_probe_mode=$(if ($lifecycle) { $lifecycle.policy.postSwitchProbeMode } else { 'unknown' })"
        Write-Output "post_switch_scope=$(if ($lifecycle) { $lifecycle.policy.postSwitchScope } else { 'unknown' })"
        Write-Output "lifecycle_status_event=$(if ($lifecycleStatus) { $lifecycleStatus.event } else { '' })"
        Write-Output "lifecycle_status_result=$(if ($lifecycleStatus) { $lifecycleStatus.result } else { '' })"
        Write-Output "lifecycle_status_title=$(if ($lifecycleStatus -and $lifecycleStatus.PSObject.Properties['conversationTitle']) { $lifecycleStatus.conversationTitle } else { '' })"
        Write-Output "lifecycle_status_cwd=$(if ($lifecycleStatus -and $lifecycleStatus.PSObject.Properties['conversationCwd']) { $lifecycleStatus.conversationCwd } else { '' })"
        Write-Output "branch_history_file=$BranchHistoryFile"
        if ($runtimeStatus -and $runtimeStatus.lastRequest) {
            $lastRequest = $runtimeStatus.lastRequest
            $lastConversationId = if ($lastRequest.PSObject.Properties["conversationId"]) { $lastRequest.conversationId } else { "" }
            $lastConversationTitle = if ($lastRequest.PSObject.Properties["conversationTitle"]) { $lastRequest.conversationTitle } else { "" }
            $lastConversationCwd = if ($lastRequest.PSObject.Properties["cwd"]) { $lastRequest.cwd } else { "" }
            $lastConversationProvider = if ($lastRequest.PSObject.Properties["modelProvider"]) { $lastRequest.modelProvider } else { "" }
            $lastNeedsRepair = if ($lastRequest.PSObject.Properties["needsRepair"]) { $lastRequest.needsRepair } else { "unknown" }
            $lastRepairStatus = if ($lastRequest.PSObject.Properties["repairStatus"]) { $lastRequest.repairStatus } else { "unknown" }
            $lastUpstreamStatus = if ($lastRequest.PSObject.Properties["upstreamStatus"]) { $lastRequest.upstreamStatus } else { "" }
            $lastOutcome = if ($lastRequest.PSObject.Properties["outcome"]) { $lastRequest.outcome } else { "" }
            $lastRequestPath = if ($lastRequest.PSObject.Properties["requestPath"]) { $lastRequest.requestPath } else { "" }
            $lastActiveProviderId = if ($lastRequest.PSObject.Properties["activeProviderId"]) { $lastRequest.activeProviderId } else { "" }
            $lastErrorCategory = if ($lastRequest.PSObject.Properties["errorCategory"]) { $lastRequest.errorCategory } else { "" }
            $lastRetrySuppressed = if ($lastRequest.PSObject.Properties["retrySuppressed"]) { $lastRequest.retrySuppressed } else { "" }
            $lastCircuitOpenUntil = if ($lastRequest.PSObject.Properties["circuitOpenUntil"]) { $lastRequest.circuitOpenUntil } else { "" }
            Write-Output "last_conversation_id=$lastConversationId"
            Write-Output "last_conversation_title=$lastConversationTitle"
            Write-Output "last_conversation_cwd=$lastConversationCwd"
            Write-Output "last_conversation_provider=$lastConversationProvider"
            Write-Output "last_needs_repair=$lastNeedsRepair"
            Write-Output "last_repair_status=$lastRepairStatus"
            Write-Output "last_upstream_status=$lastUpstreamStatus"
            Write-Output "last_request_path=$lastRequestPath"
            Write-Output "last_request_outcome=$lastOutcome"
            Write-Output "last_active_provider_id=$lastActiveProviderId"
            Write-Output "last_error_category=$lastErrorCategory"
            Write-Output "last_retry_suppressed=$lastRetrySuppressed"
            Write-Output "last_circuit_open_until=$lastCircuitOpenUntil"
        } else {
            Write-Output "last_repair_status=no-request-recorded"
        }
        $inFlightRequests = @()
        if ($runtimeStatus -and $runtimeStatus.PSObject.Properties["inFlight"] -and $runtimeStatus.inFlight) {
            $inFlightRequests = @($runtimeStatus.inFlight)
        }
        Write-Output "in_flight_request_count=$($inFlightRequests.Count)"
        foreach ($inFlightRequest in $inFlightRequests) {
            $inFlightPath = if ($inFlightRequest.PSObject.Properties["requestPath"]) { $inFlightRequest.requestPath } else { "" }
            $inFlightStartedAt = if ($inFlightRequest.PSObject.Properties["startedAt"]) { $inFlightRequest.startedAt } else { "" }
            Write-Output "in_flight_request=$inFlightPath started_at=$inFlightStartedAt"
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
