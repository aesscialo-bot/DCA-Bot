$ErrorActionPreference = 'Stop'
$composeFile = Join-Path $PSScriptRoot 'compose.yml'
$backupRoot = Join-Path $env:LOCALAPPDATA 'dca-ghostfolio\backups'
New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
icacls $backupRoot /inheritance:r /grant:r "${env:USERNAME}:(OI)(CI)F" | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$containerFile = "/tmp/ghostfolio-$stamp.dump"
$backupFile = Join-Path $backupRoot "ghostfolio-$stamp.dump"

docker compose -f $composeFile exec -T postgres pg_dump -U ghostfolio -d ghostfolio-db -Fc -f $containerFile
$postgresId = docker compose -f $composeFile ps -q postgres
docker cp "${postgresId}:$containerFile" $backupFile
docker compose -f $composeFile exec -T postgres rm -f $containerFile
$hash = (Get-FileHash -LiteralPath $backupFile -Algorithm SHA256).Hash.ToLowerInvariant()
[IO.File]::WriteAllText("$backupFile.sha256", "$hash  $([IO.Path]::GetFileName($backupFile))`n")

$testName = "dca-ghostfolio-restore-$stamp"
$testVolume = "$testName-data"
try {
  docker volume create $testVolume | Out-Null
  docker run -d --name $testName -e POSTGRES_PASSWORD=restore-test -e POSTGRES_DB=ghostfolio-db -v "${testVolume}:/var/lib/postgresql/data" postgres:15-alpine | Out-Null
  $ready = $false
  foreach ($attempt in 1..30) {
    docker exec $testName pg_isready -U postgres -d ghostfolio-db *> $null
    if ($LASTEXITCODE -eq 0) { $ready = $true; break }
    Start-Sleep -Seconds 1
  }
  if (-not $ready) { throw 'Disposable PostgreSQL did not become healthy' }
  docker cp $backupFile "${testName}:/tmp/restore.dump"
  docker exec $testName pg_restore -U postgres -d ghostfolio-db --no-owner --clean --if-exists /tmp/restore.dump
  if ($LASTEXITCODE -ne 0) { throw 'pg_restore failed in the disposable project' }
  $userCount = docker exec $testName psql -U postgres -d ghostfolio-db -Atc 'SELECT count(*) FROM "User";'
  if ($LASTEXITCODE -ne 0 -or [int]$userCount -lt 1) { throw 'restored database did not contain the local Ghostfolio user' }
  Write-Host "Backup and disposable restore test passed: $backupFile"
}
finally {
  docker rm -f $testName 2>$null | Out-Null
  docker volume rm $testVolume 2>$null | Out-Null
}
