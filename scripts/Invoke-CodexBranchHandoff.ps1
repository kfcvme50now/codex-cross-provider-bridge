[CmdletBinding()]
param(
    [string]$ConversationId = "",
    [string]$ConfigPath = "$env:USERPROFILE\.codex\config.toml",
    [string]$CodexHome = (Join-Path $env:USERPROFILE ".codex"),
    [string]$TargetProvider = "",
    [string]$TargetModel = "",
    [ValidateSet("app-server", "cli")]
    [string]$Backend = "app-server",
    [string]$ContinuePrompt = "",
    [string]$HistoryFile = "",
    [switch]$List,
    [switch]$Apply
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepositoryRoot = Split-Path $PSScriptRoot -Parent
$BranchScript = Join-Path $RepositoryRoot "src\codex_branch_handoff.py"
if (-not $HistoryFile) {
    $HistoryFile = Join-Path $RepositoryRoot "state\branch-history.jsonl"
}
if (-not (Test-Path -LiteralPath $BranchScript -PathType Leaf)) {
    throw "Branch handoff script not found: $BranchScript"
}
if (-not $List -and -not $ConversationId) {
    throw "ConversationId is required unless -List is specified"
}

$python = (Get-Command python -ErrorAction Stop).Source
$arguments = @(
    $BranchScript,
    "--codex-home",
    $CodexHome,
    "--config",
    $ConfigPath,
    "--history-file",
    $HistoryFile
)
if ($List) {
    $arguments += "--list"
} else {
    $arguments += @(
        "--conversation-id",
        $ConversationId,
        "--target-provider",
        $TargetProvider,
        "--target-model",
        $TargetModel,
        "--backend",
        $Backend,
        "--continue-prompt",
        $ContinuePrompt
    )
    if ($Apply) {
        $arguments += "--apply"
    }
}

& $python @arguments
if ($LASTEXITCODE -ne 0) {
    throw "Branch handoff failed"
}
