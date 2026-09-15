[CmdletBinding()]
param(
    [string]$BackupDirectory = "",
    [switch]$List,
    [switch]$Apply
)

$manager = Join-Path $PSScriptRoot "Manage-CodexCrossProviderBridge.ps1"
if (-not (Test-Path -LiteralPath $manager -PathType Leaf)) {
    throw "Manager script not found: $manager"
}

if ($List) {
    & $manager -Action list-migrations
    exit 0
}

if (-not $BackupDirectory) {
    throw "BackupDirectory is required; use -List to find a migration backup"
}

& $manager `
    -Action restore-migration `
    -MigrationBackupDirectory $BackupDirectory `
    -ApplyMigration:$Apply
