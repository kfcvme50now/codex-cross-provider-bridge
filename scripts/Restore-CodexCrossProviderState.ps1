[CmdletBinding()]
param(
    [string]$SnapshotId = "",
    [switch]$RestoreCcSwitchSettings,
    [switch]$ListSnapshots
)

$manager = Join-Path $PSScriptRoot "Manage-CodexCrossProviderBridge.ps1"
if (-not (Test-Path -LiteralPath $manager -PathType Leaf)) {
    throw "Manager script not found: $manager"
}

if ($ListSnapshots) {
    & $manager -Action list-snapshots
    exit 0
}

$arguments = @{
    Action = "restore"
    SnapshotId = $SnapshotId
    RestoreCcSwitchSettings = $RestoreCcSwitchSettings
}
& $manager @arguments
