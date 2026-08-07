$ErrorActionPreference = 'Stop'
$secretRoot = Join-Path $env:LOCALAPPDATA 'dca-ghostfolio'
$secretFile = Join-Path $secretRoot 'secrets.env'

$variables = @{}
Get-Content -LiteralPath $secretFile | ForEach-Object {
  if ($_ -match '^(?<key>[A-Za-z_][A-Za-z0-9_]*)=(?<value>.*)$') {
    $variables[$Matches.key] = $Matches.value
  }
}

function Write-ScopedEnvironment([string]$Name, [string[]]$Keys) {
  $lines = foreach ($key in $Keys) {
    if (-not $variables.ContainsKey($key) -or -not $variables[$key]) {
      throw "The local Ghostfolio secret store is missing $key"
    }
    "$key=$($variables[$key])"
  }
  $path = Join-Path $secretRoot $Name
  $temporaryPath = "$path.new"
  [IO.File]::WriteAllText(
    $temporaryPath,
    ($lines -join "`n") + "`n",
    [Text.UTF8Encoding]::new($false)
  )
  icacls $temporaryPath /inheritance:r /grant:r "${env:USERNAME}:F" | Out-Null
  Move-Item -LiteralPath $temporaryPath -Destination $path -Force
  icacls $path /inheritance:r /grant:r "${env:USERNAME}:F" | Out-Null
}

Write-ScopedEnvironment 'app.env' @(
  'DATABASE_URL',
  'REDIS_PASSWORD',
  'ACCESS_TOKEN_SALT',
  'JWT_SECRET_KEY'
)
Write-ScopedEnvironment 'postgres.env' @(
  'POSTGRES_DB',
  'POSTGRES_USER',
  'POSTGRES_PASSWORD'
)
Write-ScopedEnvironment 'redis.env' @('REDIS_PASSWORD')
Write-ScopedEnvironment 'sync.env' @(
  'GIST_ID',
  'GIST_TOKEN',
  'GHOSTFOLIO_SECURITY_TOKEN',
  'GHOSTFOLIO_ACCOUNT_MAP'
)

icacls $secretRoot /inheritance:r /grant:r "${env:USERNAME}:(OI)(CI)F" | Out-Null
Write-Host 'Wrote service-scoped Ghostfolio environment files with user-only ACLs.'
