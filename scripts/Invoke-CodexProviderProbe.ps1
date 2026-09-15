[CmdletBinding()]
param(
    [string]$ConfigPath = "$env:USERPROFILE\.codex\config.toml",
    [string]$CodexHome = (Join-Path $env:USERPROFILE ".codex"),
    [ValidateSet("disabled", "cli", "app-server")]
    [string]$Mode = "cli",
    [ValidateSet("preserve", "next", "all")]
    [string]$Scope = "next",
    [ValidateRange(1, 300)]
    [int]$TimeoutSeconds = 30,
    [string]$StateFile = "",
    [string]$StatusFile = "",
    [switch]$Apply
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepositoryRoot = Split-Path $PSScriptRoot -Parent
$ProbeScript = Join-Path $RepositoryRoot "src\codex_provider_probe.py"
$StateDirectory = Join-Path $RepositoryRoot "state"
if (-not $StateFile) {
    $StateFile = Join-Path $StateDirectory "provider-probe-state.json"
}
if (-not $StatusFile) {
    $StatusFile = Join-Path $StateDirectory "provider-probe-status.json"
}
if (-not (Test-Path -LiteralPath $ProbeScript -PathType Leaf)) {
    throw "Provider probe script not found: $ProbeScript"
}

$python = (Get-Command python -ErrorAction Stop).Source
$arguments = @(
    $ProbeScript,
    "--config",
    $ConfigPath,
    "--codex-home",
    $CodexHome,
    "--mode",
    $Mode,
    "--scope",
    $Scope,
    "--timeout-seconds",
    [string]$TimeoutSeconds,
    "--state-file",
    $StateFile,
    "--status-file",
    $StatusFile
)
if ($Apply) {
    $arguments += "--apply"
}

& $python @arguments
if ($LASTEXITCODE -ne 0) {
    throw "Provider probe failed"
}
