param(
  [ValidateRange(2, 365)]
  [int]$RetentionCount = 14
)

$ErrorActionPreference = 'Stop'
$taskName = 'DCA-Ghostfolio-Backup'
$installRoot = Join-Path $env:LOCALAPPDATA 'dca-ghostfolio'
$wrapper = Join-Path $installRoot 'run-backup.ps1'
$statusFile = Join-Path $installRoot 'backup-task-status.json'
$backupScript = Join-Path $PSScriptRoot 'backup-and-restore-test.ps1'

New-Item -ItemType Directory -Path $installRoot -Force | Out-Null
icacls $installRoot /inheritance:r /grant:r "${env:USERNAME}:(OI)(CI)F" | Out-Null
if ($LASTEXITCODE -ne 0) {
  throw 'Could not enforce the user-only Ghostfolio runtime ACL'
}

$escapedBackupScript = $backupScript.Replace("'", "''")
$escapedStatusFile = $statusFile.Replace("'", "''")
$wrapperTemplate = @'
$ErrorActionPreference = 'Stop'
$startedAt = [DateTimeOffset]::UtcNow
$exitCode = 1
$record = [ordered]@{
  startedAt = $startedAt.ToString('o')
  completedAt = $null
  status = 'Running'
  backupFile = $null
}
[IO.File]::WriteAllText(
  '__STATUS_FILE__',
  ($record | ConvertTo-Json -Compress),
  [Text.UTF8Encoding]::new($false)
)

try {
  & '__BACKUP_SCRIPT__' -RetentionCount __RETENTION_COUNT__ -DockerWaitAttempts 12 -DockerWaitSeconds 10
  $backupRoot = Join-Path $env:LOCALAPPDATA 'dca-ghostfolio\backups'
  $latest = Get-ChildItem -LiteralPath $backupRoot -File -Filter 'ghostfolio-*.dump' |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
  if (-not $latest -or -not (Test-Path -LiteralPath "$($latest.FullName).sha256" -PathType Leaf)) {
    throw 'Backup completed without a dump and SHA-256 sidecar'
  }
  $record.status = 'Succeeded'
  $record.backupFile = $latest.Name
  $exitCode = 0
}
catch {
  $record.status = 'Failed'
  $record.errorType = $_.Exception.GetType().FullName
  $record.errorCode = $_.Exception.Message
}
finally {
  $record.completedAt = [DateTimeOffset]::UtcNow.ToString('o')
  [IO.File]::WriteAllText(
    '__STATUS_FILE__',
    ($record | ConvertTo-Json -Compress),
    [Text.UTF8Encoding]::new($false)
  )
}

exit $exitCode
'@
$wrapperContent = $wrapperTemplate.Replace('__BACKUP_SCRIPT__', $escapedBackupScript)
$wrapperContent = $wrapperContent.Replace('__STATUS_FILE__', $escapedStatusFile)
$wrapperContent = $wrapperContent.Replace('__RETENTION_COUNT__', [string]$RetentionCount)
[IO.File]::WriteAllText($wrapper, $wrapperContent, [Text.UTF8Encoding]::new($false))

icacls $installRoot /inheritance:r /grant:r "${env:USERNAME}:(OI)(CI)F" | Out-Null
if ($LASTEXITCODE -ne 0) {
  throw 'Could not preserve the user-only Ghostfolio runtime ACL'
}

$powershell = Join-Path $PSHOME 'powershell.exe'
$actionParameters = @{
  Execute = $powershell
  Argument = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$wrapper`""
}
$action = New-ScheduledTaskAction @actionParameters
$trigger = New-ScheduledTaskTrigger -Daily -At '03:15'
$settingsParameters = @{
  AllowStartIfOnBatteries = $true
  DontStopIfGoingOnBatteries = $true
  ExecutionTimeLimit = (New-TimeSpan -Hours 2)
  MultipleInstances = 'IgnoreNew'
  RestartCount = 6
  RestartInterval = (New-TimeSpan -Minutes 10)
  StartWhenAvailable = $true
}
$settings = New-ScheduledTaskSettingsSet @settingsParameters
$principalParameters = @{
  UserId = [Security.Principal.WindowsIdentity]::GetCurrent().Name
  LogonType = 'Interactive'
  RunLevel = 'Limited'
}
$principal = New-ScheduledTaskPrincipal @principalParameters

$registrationParameters = @{
  TaskName = $taskName
  Action = $action
  Trigger = $trigger
  Settings = $settings
  Principal = $principal
  Force = $true
}
Register-ScheduledTask @registrationParameters | Out-Null

$registered = Get-ScheduledTask -TaskName $taskName
$registeredInfo = Get-ScheduledTaskInfo -TaskName $taskName
if (-not $registered.Settings.StartWhenAvailable) {
  throw 'Scheduled backup task was registered without catch-up behavior'
}
if ($registered.Settings.RestartCount -ne 6) {
  throw 'Scheduled backup task was registered without bounded retries'
}
if ($registered.Settings.MultipleInstances -ne 'IgnoreNew') {
  throw 'Scheduled backup task was registered without overlap protection'
}

[pscustomobject]@{
  TaskName = $taskName
  State = $registered.State
  NextRunTime = $registeredInfo.NextRunTime
  StartWhenAvailable = $registered.Settings.StartWhenAvailable
  RestartCount = $registered.Settings.RestartCount
  RestartInterval = $registered.Settings.RestartInterval
  MultipleInstances = $registered.Settings.MultipleInstances
  RetentionCount = $RetentionCount
}
