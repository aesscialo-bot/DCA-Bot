param(
  [Parameter(Mandatory = $true)][string]$GistId,
  [string]$GistToken = $env:DCA_GHOSTFOLIO_GIST_TOKEN
)

$ErrorActionPreference = 'Stop'
if (-not $GistToken) { throw 'GistToken or DCA_GHOSTFOLIO_GIST_TOKEN is required' }
$secretRoot = Join-Path $env:LOCALAPPDATA 'dca-ghostfolio'
$secretFile = Join-Path $secretRoot 'secrets.env'
New-Item -ItemType Directory -Path $secretRoot -Force | Out-Null

function New-HexSecret([int]$Bytes = 32) {
  $buffer = New-Object byte[] $Bytes
  $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
  try { $generator.GetBytes($buffer) } finally { $generator.Dispose() }
  return ([BitConverter]::ToString($buffer) -replace '-', '').ToLowerInvariant()
}

$postgres = New-HexSecret 24
$redis = New-HexSecret 24
$salt = New-HexSecret 32
$jwt = New-HexSecret 32
$content = @(
  'COMPOSE_PROJECT_NAME=dca-ghostfolio'
  'POSTGRES_DB=ghostfolio-db'
  'POSTGRES_USER=ghostfolio'
  "POSTGRES_PASSWORD=$postgres"
  "DATABASE_URL=postgresql://ghostfolio:$postgres@postgres:5432/ghostfolio-db?connect_timeout=300"
  "REDIS_PASSWORD=$redis"
  "ACCESS_TOKEN_SALT=$salt"
  "JWT_SECRET_KEY=$jwt"
  "GIST_ID=$GistId"
  "GIST_TOKEN=$GistToken"
  'GHOSTFOLIO_SECURITY_TOKEN=configure-after-first-local-login'
  'GHOSTFOLIO_ACCOUNT_MAP={}'
) -join "`n"
[IO.File]::WriteAllText($secretFile, $content + "`n", [Text.UTF8Encoding]::new($false))
icacls $secretRoot /inheritance:r /grant:r "${env:USERNAME}:(OI)(CI)F" | Out-Null
[Environment]::SetEnvironmentVariable('DCA_GHOSTFOLIO_SECRETS_FILE', $secretFile, 'User')
$env:DCA_GHOSTFOLIO_SECRETS_FILE = $secretFile

docker compose -f (Join-Path $PSScriptRoot 'compose.yml') config --quiet
docker compose -f (Join-Path $PSScriptRoot 'compose.yml') up -d --build
Write-Host "Ghostfolio is starting at http://127.0.0.1:3333"
Write-Host "Secrets: $secretFile (user-only ACL). Configure the local security token and account map after first login."
