$ErrorActionPreference = 'Stop'
$installRoot = Join-Path $env:LOCALAPPDATA 'dca-ghostfolio'
$wrapper = Join-Path $installRoot 'run-backup.ps1'
$backupScript = Join-Path $PSScriptRoot 'backup-and-restore-test.ps1'
$escaped = $backupScript.Replace("'", "''")
$wrapperContent = "`$env:DCA_GHOSTFOLIO_SECRETS_FILE = Join-Path `$env:LOCALAPPDATA 'dca-ghostfolio\secrets.env'`n& '$escaped'`nif (`$LASTEXITCODE -ne 0) { exit `$LASTEXITCODE }`n"
[IO.File]::WriteAllText($wrapper, $wrapperContent, [Text.UTF8Encoding]::new($false))
icacls $installRoot /inheritance:r /grant:r "${env:USERNAME}:(OI)(CI)F" | Out-Null
$taskCommand = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File $wrapper"
schtasks.exe /Create /TN 'DCA-Ghostfolio-Backup' /TR $taskCommand /SC DAILY /ST 03:15 /F | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not create DCA-Ghostfolio-Backup scheduled task' }
schtasks.exe /Query /TN 'DCA-Ghostfolio-Backup' /FO LIST
