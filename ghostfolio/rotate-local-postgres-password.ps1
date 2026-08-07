$ErrorActionPreference = 'Stop'
$secretFile = Join-Path $env:LOCALAPPDATA 'dca-ghostfolio\secrets.env'
$composeFile = Join-Path $PSScriptRoot 'compose.yml'
$env:DCA_GHOSTFOLIO_SECRETS_FILE = $secretFile

$variables = @{}
Get-Content -LiteralPath $secretFile | ForEach-Object {
  if ($_ -match '^(?<key>[A-Za-z_][A-Za-z0-9_]*)=(?<value>.*)$') {
    $variables[$Matches.key] = $Matches.value
  }
}
$databaseName = $variables.POSTGRES_DB
$databaseUser = $variables.POSTGRES_USER
$oldPassword = $variables.POSTGRES_PASSWORD
$oldContent = [IO.File]::ReadAllText($secretFile)
if ($databaseName -notmatch '^[A-Za-z_][A-Za-z0-9_-]*$') { throw 'POSTGRES_DB is invalid' }
if ($databaseUser -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { throw 'POSTGRES_USER is invalid' }
if ($oldPassword -notmatch '^[0-9a-f]{48}$') { throw 'POSTGRES_PASSWORD is invalid' }
if ($variables.DATABASE_URL -notmatch '^postgresql://[^:]+:[^@]+@postgres:5432/[^?]+\?connect_timeout=300$') {
  throw 'DATABASE_URL is invalid'
}

$buffer = New-Object byte[] 24
$generator = [Security.Cryptography.RandomNumberGenerator]::Create()
try { $generator.GetBytes($buffer) } finally { $generator.Dispose() }
$newPassword = ([BitConverter]::ToString($buffer) -replace '-', '').ToLowerInvariant()

function Set-DatabasePassword([string]$Password) {
  if ($Password -notmatch '^[0-9a-f]{48}$') { throw 'Generated database password is invalid' }
  "ALTER ROLE $databaseUser WITH PASSWORD '$Password';" |
    docker compose -f $composeFile exec -T postgres psql -v ON_ERROR_STOP=1 -U $databaseUser -d $databaseName | Out-Null
  if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL rejected the password rotation' }
}

Set-DatabasePassword $newPassword
try {
  $content = [IO.File]::ReadAllText($secretFile)
  $content = [regex]::Replace($content, '(?m)^POSTGRES_PASSWORD=.*$', "POSTGRES_PASSWORD=$newPassword")
  $content = [regex]::Replace(
    $content,
    '(?m)^DATABASE_URL=.*$',
    "DATABASE_URL=postgresql://${databaseUser}:$newPassword@postgres:5432/${databaseName}?connect_timeout=300"
  )
  $temporaryFile = "$secretFile.new"
  [IO.File]::WriteAllText($temporaryFile, $content, [Text.UTF8Encoding]::new($false))
  icacls $temporaryFile /inheritance:r /grant:r "${env:USERNAME}:F" | Out-Null
  Move-Item -LiteralPath $temporaryFile -Destination $secretFile -Force
  icacls $secretFile /inheritance:r /grant:r "${env:USERNAME}:F" | Out-Null
  & (Join-Path $PSScriptRoot 'write-service-env.ps1') | Out-Null
}
catch {
  try {
    [IO.File]::WriteAllText($secretFile, $oldContent, [Text.UTF8Encoding]::new($false))
    icacls $secretFile /inheritance:r /grant:r "${env:USERNAME}:F" | Out-Null
    & (Join-Path $PSScriptRoot 'write-service-env.ps1') | Out-Null
  }
  finally {
    Set-DatabasePassword $oldPassword
  }
  throw
}

docker compose -f $composeFile up -d --force-recreate postgres app | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Ghostfolio services did not accept the rotated credential' }
Write-Host 'The local Ghostfolio PostgreSQL credential was rotated without printing it.'
