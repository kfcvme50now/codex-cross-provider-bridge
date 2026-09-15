Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$script:CodexBridgeTaskName = "CodexCrossProviderBridge"
$script:CodexBridgeAutomationTaskName = "CodexCrossProviderBridgeAutoRepair"
$script:CodexBridgeBackupRoot = if ($env:CODEX_BRIDGE_BACKUP_ROOT) {
    [System.IO.Path]::GetFullPath($env:CODEX_BRIDGE_BACKUP_ROOT)
} else {
    Join-Path $env:USERPROFILE ".codex\backups\codex-cross-provider-bridge"
}
$script:CodexBridgeSnapshotRoot = Join-Path $script:CodexBridgeBackupRoot "snapshots"
$script:CodexBridgeIndexPath = Join-Path $script:CodexBridgeBackupRoot "index.jsonl"

function Get-NormalizedPath {
    param([Parameter(Mandatory)][string]$Path)
    return [System.IO.Path]::GetFullPath($Path)
}

function Get-Sha256Hex {
    param([Parameter(Mandatory)][string]$Path)

    if (Get-Command -Name Get-FileHash -ErrorAction SilentlyContinue) {
        return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash
    }

    # Windows PowerShell can inherit a PowerShell 7 module path, where
    # Get-FileHash is not defined; fall back to the .NET implementation.
    $stream = [System.IO.File]::OpenRead($Path)
    try {
        $sha = [System.Security.Cryptography.SHA256]::Create()
        try {
            return [System.BitConverter]::ToString($sha.ComputeHash($stream)).Replace("-", "")
        } finally {
            $sha.Dispose()
        }
    } finally {
        $stream.Dispose()
    }
}

function Get-FileHashOrNull {
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $null
    }
    return (Get-Sha256Hex -Path $Path)
}

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][AllowEmptyString()][string]$Content
    )
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $encoding)
}

function Test-TomlFile {
    param([Parameter(Mandatory)][string]$Path)
    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) {
        return $true
    }

    $env:CODEX_BRIDGE_TOML_TEST_PATH = $Path
    try {
        @'
import os
import tomllib

with open(os.environ["CODEX_BRIDGE_TOML_TEST_PATH"], "rb") as handle:
    tomllib.load(handle)
'@ | & $python.Source -
        return $LASTEXITCODE -eq 0
    } finally {
        Remove-Item Env:CODEX_BRIDGE_TOML_TEST_PATH -ErrorAction SilentlyContinue
    }
}

function Get-ScheduledTaskXmlOrNull {
    param([Parameter(Mandatory)][string]$TaskName)
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        return $null
    }
    return (Export-ScheduledTask -TaskName $TaskName)
}

function New-CodexBridgeSnapshot {
    [CmdletBinding(SupportsShouldProcess)]
    param(
        [Parameter(Mandatory)][string]$Reason,
        [Parameter(Mandatory)][string]$ConfigPath,
        [Parameter(Mandatory)][string]$BridgeScript,
        [string]$SourceConfigPath = "",
        [string]$TaskName = $script:CodexBridgeTaskName,
        [string]$CcSwitchSettingsPath = "$env:USERPROFILE\.cc-switch\settings.json",
        [string]$BridgeStateDirectory = (Join-Path (Split-Path $PSScriptRoot -Parent) "state"),
        [switch]$SkipTaskState
    )

    if (-not $PSCmdlet.ShouldProcess($ConfigPath, "Create bridge snapshot")) {
        return $null
    }

    $createdAt = (Get-Date).ToUniversalTime()
    $snapshotId = "{0}-{1}" -f $createdAt.ToString("yyyyMMddTHHmmssfffZ"), ([guid]::NewGuid().ToString("N").Substring(0, 8))
    $snapshotDirectory = Join-Path $script:CodexBridgeSnapshotRoot $snapshotId
    New-Item -ItemType Directory -Path $snapshotDirectory -Force | Out-Null

    $files = @()
    if (-not $SourceConfigPath) {
        $SourceConfigPath = $ConfigPath
    }

    $configBackupPath = Join-Path $snapshotDirectory "config.toml"
    if (Test-Path -LiteralPath $SourceConfigPath -PathType Leaf) {
        Copy-Item -LiteralPath $SourceConfigPath -Destination $configBackupPath
        $files += [pscustomobject]@{
            Kind = "codex-config"
            File = "config.toml"
            Source = Get-NormalizedPath -Path $SourceConfigPath
            SHA256 = Get-FileHashOrNull -Path $configBackupPath
        }
    }

    $settingsBackupPath = Join-Path $snapshotDirectory "cc-switch-settings.json"
    if (Test-Path -LiteralPath $CcSwitchSettingsPath -PathType Leaf) {
        Copy-Item -LiteralPath $CcSwitchSettingsPath -Destination $settingsBackupPath
        $files += [pscustomobject]@{
            Kind = "cc-switch-settings"
            File = "cc-switch-settings.json"
            Source = Get-NormalizedPath -Path $CcSwitchSettingsPath
            SHA256 = Get-FileHashOrNull -Path $settingsBackupPath
        }
    }

    $taskXml = if ($SkipTaskState) {
        $null
    } else {
        Get-ScheduledTaskXmlOrNull -TaskName $TaskName
    }
    if ($taskXml) {
        $taskPath = Join-Path $snapshotDirectory "scheduled-task.xml"
        Write-Utf8NoBom -Path $taskPath -Content $taskXml
        $files += [pscustomobject]@{
            Kind = "scheduled-task"
            File = "scheduled-task.xml"
            Source = $TaskName
            SHA256 = Get-FileHashOrNull -Path $taskPath
        }
    }

    foreach ($stateName in @("policy.json", "status.json")) {
        $stateSource = Join-Path $BridgeStateDirectory $stateName
        if (Test-Path -LiteralPath $stateSource -PathType Leaf) {
            $stateTarget = Join-Path $snapshotDirectory $stateName
            Copy-Item -LiteralPath $stateSource -Destination $stateTarget
            $files += [pscustomobject]@{
                Kind = "bridge-state"
                File = $stateName
                Source = Get-NormalizedPath -Path $stateSource
                SHA256 = Get-FileHashOrNull -Path $stateTarget
            }
        }
    }

    $manifest = [pscustomobject]@{
        SchemaVersion = 1
        SnapshotId = $snapshotId
        CreatedAtUtc = $createdAt.ToString("o")
        Reason = $Reason
        ConfigPath = Get-NormalizedPath -Path $ConfigPath
        ConfigExists = Test-Path -LiteralPath $SourceConfigPath -PathType Leaf
        SourceConfigPath = Get-NormalizedPath -Path $SourceConfigPath
        CcSwitchSettingsPath = Get-NormalizedPath -Path $CcSwitchSettingsPath
        BridgeStateDirectory = Get-NormalizedPath -Path $BridgeStateDirectory
        TaskName = $TaskName
        TaskPresent = [bool]$taskXml
        BridgeScriptPath = Get-NormalizedPath -Path $BridgeScript
        BridgeScriptSHA256 = Get-FileHashOrNull -Path $BridgeScript
        Files = $files
    }

    $manifestPath = Join-Path $snapshotDirectory "manifest.json"
    $manifest | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $manifestPath -Encoding utf8
    $manifest | ConvertTo-Json -Depth 10 -Compress | Add-Content -LiteralPath $script:CodexBridgeIndexPath -Encoding utf8

    return [pscustomobject]@{
        SnapshotId = $snapshotId
        Directory = $snapshotDirectory
        ManifestPath = $manifestPath
        Reason = $Reason
        CreatedAtUtc = $createdAt
    }
}

function Get-CodexBridgeSnapshotList {
    if (-not (Test-Path -LiteralPath $script:CodexBridgeSnapshotRoot)) {
        return @()
    }

    return Get-ChildItem -LiteralPath $script:CodexBridgeSnapshotRoot -Directory |
        Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "manifest.json") } |
        ForEach-Object {
            $manifest = Get-Content -Raw -LiteralPath (Join-Path $_.FullName "manifest.json") | ConvertFrom-Json
            [pscustomobject]@{
                SnapshotId = $manifest.SnapshotId
                Directory = $_.FullName
                CreatedAtUtc = [datetime]$manifest.CreatedAtUtc
                Reason = $manifest.Reason
                ConfigPath = $manifest.ConfigPath
                TaskPresent = [bool]$manifest.TaskPresent
            }
        } |
        Sort-Object CreatedAtUtc -Descending
}

function Get-CodexBridgeSnapshot {
    param(
        [string]$SnapshotId = "",
        [string]$Reason = "",
        [string]$ConfigPath = ""
    )

    $snapshots = Get-CodexBridgeSnapshotList
    if ($SnapshotId) {
        $snapshots = $snapshots | Where-Object SnapshotId -eq $SnapshotId
    }
    if ($Reason) {
        $snapshots = $snapshots | Where-Object Reason -eq $Reason
    }
    if ($ConfigPath) {
        $normalized = Get-NormalizedPath -Path $ConfigPath
        $snapshots = $snapshots | Where-Object ConfigPath -eq $normalized
    }
    return $snapshots | Select-Object -First 1
}

function Restore-CodexBridgeSnapshot {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$SnapshotDirectory,
        [switch]$RestoreCcSwitchSettings,
        [switch]$KeepCurrentTask
    )

    $manifestPath = Join-Path $SnapshotDirectory "manifest.json"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "Snapshot manifest not found: $manifestPath"
    }
    $manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json

    $configSource = Join-Path $SnapshotDirectory "config.toml"
    if (-not (Test-Path -LiteralPath $configSource -PathType Leaf)) {
        throw "Snapshot does not contain config.toml: $SnapshotDirectory"
    }
    if (-not (Test-TomlFile -Path $configSource)) {
        throw "Snapshot config.toml is not valid TOML: $configSource"
    }

    $targetConfigPath = $manifest.ConfigPath
    $preRestore = New-CodexBridgeSnapshot `
        -Reason "pre-restore" `
        -ConfigPath $targetConfigPath `
        -BridgeScript (Join-Path (Split-Path $PSScriptRoot -Parent) "src\codex_cross_provider_bridge.py") `
        -TaskName $manifest.TaskName `
        -CcSwitchSettingsPath $manifest.CcSwitchSettingsPath

    try {
        if (-not $KeepCurrentTask) {
            if (Get-ScheduledTask -TaskName $manifest.TaskName -ErrorAction SilentlyContinue) {
                Stop-ScheduledTask -TaskName $manifest.TaskName -ErrorAction SilentlyContinue
                Unregister-ScheduledTask -TaskName $manifest.TaskName -Confirm:$false
            }

            $taskSource = Join-Path $SnapshotDirectory "scheduled-task.xml"
            if (Test-Path -LiteralPath $taskSource -PathType Leaf) {
                Register-ScheduledTask -Xml (Get-Content -Raw -LiteralPath $taskSource) -TaskName $manifest.TaskName -Force | Out-Null
            }
        }

        $temporaryConfig = "$targetConfigPath.restore.$PID.tmp"
        Copy-Item -LiteralPath $configSource -Destination $temporaryConfig -Force
        if (-not (Test-TomlFile -Path $temporaryConfig)) {
            throw "Temporary restored config is not valid TOML"
        }
        Move-Item -LiteralPath $temporaryConfig -Destination $targetConfigPath -Force

        if ($RestoreCcSwitchSettings) {
            $settingsSource = Join-Path $SnapshotDirectory "cc-switch-settings.json"
            $settingsTarget = $manifest.CcSwitchSettingsPath
            if (Test-Path -LiteralPath $settingsSource -PathType Leaf) {
                $temporarySettings = "$settingsTarget.restore.$PID.tmp"
                Copy-Item -LiteralPath $settingsSource -Destination $temporarySettings -Force
                Move-Item -LiteralPath $temporarySettings -Destination $settingsTarget -Force
            }
        }
    } catch {
        $rollbackConfig = Join-Path $preRestore.Directory "config.toml"
        if (Test-Path -LiteralPath $rollbackConfig -PathType Leaf) {
            Copy-Item -LiteralPath $rollbackConfig -Destination $targetConfigPath -Force
        }
        throw
    }

    return [pscustomobject]@{
        RestoredSnapshotId = $manifest.SnapshotId
        RestoredConfigPath = $targetConfigPath
        PreRestoreSnapshotId = $preRestore.SnapshotId
        RestoredTask = [bool]$manifest.TaskPresent -and -not $KeepCurrentTask
        RestoredCcSwitchSettings = [bool]$RestoreCcSwitchSettings
    }
}
