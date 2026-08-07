$ErrorActionPreference = 'Stop'
$secretFile = Join-Path $env:LOCALAPPDATA 'dca-ghostfolio\secrets.env'
$composeFile = Join-Path $PSScriptRoot 'compose.yml'
$oldContent = [IO.File]::ReadAllText($secretFile)

function New-HexSecret([int]$Bytes) {
  $buffer = New-Object byte[] $Bytes
  $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
  try { $generator.GetBytes($buffer) } finally { $generator.Dispose() }
  return ([BitConverter]::ToString($buffer) -replace '-', '').ToLowerInvariant()
}

$newRedisPassword = New-HexSecret 24
$newJwtSecret = New-HexSecret 32
try {
  $content = [regex]::Replace(
    $oldContent,
    '(?m)^REDIS_PASSWORD=.*$',
    "REDIS_PASSWORD=$newRedisPassword"
  )
  $content = [regex]::Replace(
    $content,
    '(?m)^JWT_SECRET_KEY=.*$',
    "JWT_SECRET_KEY=$newJwtSecret"
  )
  $temporaryFile = "$secretFile.new"
  [IO.File]::WriteAllText($temporaryFile, $content, [Text.UTF8Encoding]::new($false))
  icacls $temporaryFile /inheritance:r /grant:r "${env:USERNAME}:F" | Out-Null
  Move-Item -LiteralPath $temporaryFile -Destination $secretFile -Force
  icacls $secretFile /inheritance:r /grant:r "${env:USERNAME}:F" | Out-Null
  & (Join-Path $PSScriptRoot 'write-service-env.ps1') | Out-Null
  docker compose -f $composeFile up -d --force-recreate redis app | Out-Null
  if ($LASTEXITCODE -ne 0) { throw 'Ghostfolio rejected the rotated runtime secrets' }
}
catch {
  [IO.File]::WriteAllText($secretFile, $oldContent, [Text.UTF8Encoding]::new($false))
  icacls $secretFile /inheritance:r /grant:r "${env:USERNAME}:F" | Out-Null
  & (Join-Path $PSScriptRoot 'write-service-env.ps1') | Out-Null
  docker compose -f $composeFile up -d --force-recreate redis app | Out-Null
  throw
}

Write-Host 'The local Ghostfolio Redis and JWT credentials were rotated without printing them.'
