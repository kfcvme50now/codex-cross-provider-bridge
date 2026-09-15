[CmdletBinding()]
param(
    [ValidateSet(
        "status",
        "install-hooks",
        "uninstall-hooks",
        "list-backups",
        "restore-hooks",
        "list-policy-backups",
        "restore-policy-backup",
        "set-policy"
    )]
    [string]$Action = "status",
    [string]$CodexHome = (Join-Path $env:USERPROFILE ".codex"),
    [string]$ConfigPath = "$env:USERPROFILE\.codex\config.toml",
    [string]$PolicyPath = "",
    [string]$StatusFile = "",
    [ValidateSet(
        "disabled",
        "inspect",
        "repair-and-continue",
        "repair-and-stop",
        "repair-and-branch",
        "branch-only",
        "block-only"
    )]
    [string]$CompactMode = "",
    [ValidateSet("true", "false")]
    [string]$AutoBranch = "false",
    [ValidateSet("app-server", "cli")]
    [string]$BranchBackend = "app-server",
    [ValidateSet("disabled", "cli", "app-server")]
    [string]$PostSwitchProbeMode = "disabled",
    [ValidateSet("preserve", "next", "all")]
    [string]$PostSwitchScope = "preserve",
    [ValidateSet("disabled", "repair", "repair-and-probe")]
    [string]$SessionStartMode = "repair",
    [ValidateSet("disabled", "inspect", "repair")]
    [string]$RouteRepairMode = "repair",
    [string]$BridgeUrl = "http://127.0.0.1:15722/v1",
    [ValidateRange(1, 300)]
    [int]$ProbeTimeoutSeconds = 30,
    [string]$BackupDirectory = "",
    [string]$BackupFile = "",
    [switch]$Apply
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepositoryRoot = Split-Path $PSScriptRoot -Parent
$ControlScript = Join-Path $RepositoryRoot "src\codex_lifecycle_control.py"
$LifecycleScript = Join-Path $RepositoryRoot "src\codex_lifecycle_hook.py"
$StateDirectory = Join-Path $RepositoryRoot "state"
if (-not $PolicyPath) {
    $PolicyPath = Join-Path $StateDirectory "lifecycle-policy.json"
}
if (-not $StatusFile) {
    $StatusFile = Join-Path $StateDirectory "lifecycle-status.json"
}
if (-not (Test-Path -LiteralPath $ControlScript -PathType Leaf)) {
    throw "Lifecycle control script not found: $ControlScript"
}

$python = (Get-Command python -ErrorAction Stop).Source
$arguments = @(
    $ControlScript,
    $Action,
    "--codex-home",
    $CodexHome,
    "--config",
    $ConfigPath,
    "--policy",
    $PolicyPath,
    "--status-file",
    $StatusFile,
    "--lifecycle-script",
    $LifecycleScript,
    "--python-executable",
    $python
)

if ($Action -eq "install-hooks" -or $Action -eq "set-policy") {
    $arguments += @("--bridge-url", $BridgeUrl)
}

if ($Action -eq "set-policy") {
    if ($PSBoundParameters.ContainsKey("CompactMode") -and $CompactMode) {
        $arguments += @("--compact-mode", $CompactMode)
    }
    if ($PSBoundParameters.ContainsKey("AutoBranch")) {
        $arguments += @("--auto-branch", $AutoBranch)
    }
    if ($PSBoundParameters.ContainsKey("BranchBackend")) {
        $arguments += @("--branch-backend", $BranchBackend)
    }
    if ($PSBoundParameters.ContainsKey("PostSwitchProbeMode")) {
        $arguments += @("--post-switch-probe-mode", $PostSwitchProbeMode)
    }
    if ($PSBoundParameters.ContainsKey("PostSwitchScope")) {
        $arguments += @("--post-switch-scope", $PostSwitchScope)
    }
    if ($PSBoundParameters.ContainsKey("SessionStartMode")) {
        $arguments += @("--session-start-mode", $SessionStartMode)
    }
    if ($PSBoundParameters.ContainsKey("RouteRepairMode")) {
        $arguments += @("--route-repair-mode", $RouteRepairMode)
    }
    if ($PSBoundParameters.ContainsKey("ProbeTimeoutSeconds")) {
        $arguments += @("--probe-timeout-seconds", [string]$ProbeTimeoutSeconds)
    }
}
if ($Action -eq "restore-hooks") {
    if (-not $BackupDirectory) {
        throw "BackupDirectory is required for restore-hooks"
    }
    $arguments += @("--backup-directory", $BackupDirectory)
}
if ($Action -eq "restore-policy-backup") {
    if (-not $BackupFile) {
        throw "BackupFile is required for restore-policy-backup"
    }
    $arguments += @("--backup-file", $BackupFile)
}
if ($Apply) {
    $arguments += "--apply"
}

& $python @arguments
if ($LASTEXITCODE -ne 0) {
    throw "Lifecycle hook management failed"
}
