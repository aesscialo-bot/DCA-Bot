param(
  [ValidateRange(2, 365)]
  [int]$RetentionCount = 14,

  [ValidateRange(1, 60)]
  [int]$DockerWaitAttempts = 12,

  [ValidateRange(1, 60)]
  [int]$DockerWaitSeconds = 10
)

$ErrorActionPreference = 'Stop'
$composeFile = Join-Path $PSScriptRoot 'compose.yml'
$backupRoot = Join-Path $env:LOCALAPPDATA 'dca-ghostfolio\backups'

function Assert-NativeSuccess {
  param([Parameter(Mandatory = $true)][string]$Message)

  if ($LASTEXITCODE -ne 0) {
    throw $Message
  }
}

function Wait-GhostfolioPostgres {
  $lastFailure = 'docker-info'
  foreach ($attempt in 1..$DockerWaitAttempts) {
    $dockerInfoOutput = @(docker info --format '{{.ServerVersion}}' 2>$null)
    $dockerInfoExit = $LASTEXITCODE
    if ($dockerInfoExit -eq 0) {
      $lastFailure = 'compose-postgres'
      $candidateOutput = @(docker compose -f $composeFile ps -q postgres 2>$null)
      $composeExit = $LASTEXITCODE
      $candidate = [string]($candidateOutput | Select-Object -First 1)
      if ($composeExit -eq 0 -and $candidate.Trim()) {
        $candidate = $candidate.Trim()
        $lastFailure = 'postgres-health'
        $healthOutput = @(
          docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' $candidate 2>$null
        )
        $inspectExit = $LASTEXITCODE
        $health = [string]($healthOutput | Select-Object -First 1)
        if ($inspectExit -eq 0 -and $health.Trim() -eq 'healthy') {
          return $candidate
        }
      }
    }

    if ($attempt -lt $DockerWaitAttempts) {
      Start-Sleep -Seconds $DockerWaitSeconds
    }
  }

  $waitSeconds = $DockerWaitAttempts * $DockerWaitSeconds
  throw "backup-preflight:$lastFailure unavailable after $waitSeconds seconds"
}

function Remove-ExpiredBackups {
  $resolvedRoot = [IO.Path]::GetFullPath($backupRoot).TrimEnd('\') + '\'
  $backups = @(
    Get-ChildItem -LiteralPath $backupRoot -File -Filter 'ghostfolio-*.dump' |
      Where-Object { $_.Name -match '^ghostfolio-\d{8}-\d{6}\.dump$' } |
      Sort-Object LastWriteTime -Descending
  )

  foreach ($backup in @($backups | Select-Object -Skip $RetentionCount)) {
    $resolvedBackup = [IO.Path]::GetFullPath($backup.FullName)
    if (-not $resolvedBackup.StartsWith($resolvedRoot, [StringComparison]::OrdinalIgnoreCase)) {
      throw "Refusing to remove a backup outside $backupRoot"
    }

    Remove-Item -LiteralPath $resolvedBackup -Force
    $sidecar = "$resolvedBackup.sha256"
    if (Test-Path -LiteralPath $sidecar -PathType Leaf) {
      Remove-Item -LiteralPath $sidecar -Force
    }
  }
}

New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
icacls $backupRoot /inheritance:r /grant:r "${env:USERNAME}:(OI)(CI)F" | Out-Null
Assert-NativeSuccess 'Could not enforce the user-only backup directory ACL'

$postgresId = Wait-GhostfolioPostgres
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$containerFile = "/tmp/ghostfolio-$stamp.dump"
$backupFile = Join-Path $backupRoot "ghostfolio-$stamp.dump"
$containerDumpCreated = $false

try {
  docker exec $postgresId pg_dump -U ghostfolio -d ghostfolio-db -Fc -f $containerFile *> $null
  Assert-NativeSuccess 'Ghostfolio pg_dump failed'
  $containerDumpCreated = $true

  docker cp "${postgresId}:$containerFile" $backupFile *> $null
  Assert-NativeSuccess 'Could not copy the Ghostfolio dump to the protected backup directory'
}
finally {
  if ($containerDumpCreated) {
    docker exec $postgresId rm -f $containerFile *> $null
  }
}

$hash = (Get-FileHash -LiteralPath $backupFile -Algorithm SHA256).Hash.ToLowerInvariant()
[IO.File]::WriteAllText(
  "$backupFile.sha256",
  "$hash  $([IO.Path]::GetFileName($backupFile))`n",
  [Text.UTF8Encoding]::new($false)
)

$postgresImageOutput = @(docker inspect --format '{{.Image}}' $postgresId 2>$null)
Assert-NativeSuccess 'Could not resolve the deployed PostgreSQL image'
$postgresImageId = [string]($postgresImageOutput | Select-Object -First 1)
$postgresImageId = $postgresImageId.Trim()

$testName = "dca-ghostfolio-restore-$stamp"
$testVolume = "$testName-data"
try {
  docker volume create $testVolume *> $null
  Assert-NativeSuccess 'Could not create the disposable restore-test volume'

  docker run -d --name $testName -e POSTGRES_PASSWORD=restore-test -e POSTGRES_DB=ghostfolio-db -v "${testVolume}:/var/lib/postgresql/data" $postgresImageId *> $null
  Assert-NativeSuccess 'Could not start disposable PostgreSQL for restore verification'

  $ready = $false
  $consecutiveReady = 0
  foreach ($attempt in 1..60) {
    docker exec $testName pg_isready -U postgres -d ghostfolio-db *> $null
    if ($LASTEXITCODE -eq 0) {
      $consecutiveReady++
      if ($consecutiveReady -ge 3) {
        $ready = $true
        break
      }
    }
    else {
      $consecutiveReady = 0
    }
    Start-Sleep -Seconds 1
  }
  if (-not $ready) {
    throw 'Disposable PostgreSQL did not become healthy'
  }

  docker cp $backupFile "${testName}:/tmp/restore.dump" *> $null
  Assert-NativeSuccess 'Could not copy the backup into disposable PostgreSQL'

  docker exec $testName pg_restore -U postgres -d ghostfolio-db --no-owner --clean --if-exists /tmp/restore.dump *> $null
  Assert-NativeSuccess 'pg_restore failed in the disposable project'

  $userCountOutput = @(
    docker exec $testName psql -U postgres -d ghostfolio-db -Atc 'SELECT count(*) FROM "User";' 2>$null
  )
  Assert-NativeSuccess 'Could not validate the restored Ghostfolio database'
  $userCount = [string]($userCountOutput | Select-Object -First 1)
  if ([int]$userCount.Trim() -lt 1) {
    throw 'Restored database did not contain a local Ghostfolio user'
  }
}
finally {
  docker rm -f $testName 2>$null | Out-Null
  docker volume rm $testVolume 2>$null | Out-Null
}

# Retention is intentionally last: a failed dump or restore test never removes a
# previously verified backup.
Remove-ExpiredBackups
Write-Host "Backup and disposable restore test passed: $backupFile (retaining latest $RetentionCount)"
