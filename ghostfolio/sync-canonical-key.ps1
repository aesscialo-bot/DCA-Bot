param(
  [int]$MinimumHoldings = 0
)

$ErrorActionPreference = 'Stop'
$secretFile = Join-Path $env:LOCALAPPDATA 'dca-ghostfolio\secrets.env'
$repositoryRoot = Split-Path $PSScriptRoot -Parent
$gladosRoot = Split-Path $repositoryRoot -Parent
$ghostfolioRoot = Join-Path $gladosRoot 'Ghostfolio'
$keyPath = Join-Path $ghostfolioRoot 'Key.txt'
$archiveDirectory = Join-Path $env:LOCALAPPDATA 'dca-ghostfolio\retired-keys'
$expectedHoldingSymbols = @('bitcoin', 'HYPE32196USD', 'solana')

$variables = @{}
Get-Content -LiteralPath $secretFile | ForEach-Object {
  if ($_ -match '^(?<key>[A-Za-z_][A-Za-z0-9_]*)=(?<value>.*)$') {
    $variables[$Matches.key] = $Matches.value
  }
}
$canonicalToken = $variables.GHOSTFOLIO_SECURITY_TOKEN
if ($canonicalToken -notmatch '^[0-9a-f]{128}$') {
  throw 'The configured Ghostfolio security token is invalid'
}

function Get-GhostfolioIdentity([string]$Token) {
  $auth = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:3333/api/v1/auth/anonymous' -TimeoutSec 15 -ContentType 'application/json' -Body (@{ accessToken = $Token } | ConvertTo-Json -Compress)
  if (-not $auth.authToken) { throw 'Ghostfolio did not return an authentication token' }
  $headers = @{ Authorization = "Bearer $($auth.authToken)" }
  $user = Invoke-RestMethod -Method Get -Uri 'http://127.0.0.1:3333/api/v1/user' -TimeoutSec 30 -Headers $headers
  $holdings = Invoke-RestMethod -Method Get -Uri 'http://127.0.0.1:3333/api/v1/portfolio/holdings' -TimeoutSec 120 -Headers $headers
  return [pscustomobject]@{
    id = $user.id
    holdingCount = @($holdings.holdings).Count
    holdingSymbols = @($holdings.holdings | ForEach-Object { $_.assetProfile.symbol } | Sort-Object -Unique)
  }
}

$canonicalIdentity = Get-GhostfolioIdentity $canonicalToken
if (-not $canonicalIdentity.id) { throw 'The configured Ghostfolio token has no user identity' }
if ($canonicalIdentity.holdingCount -lt $MinimumHoldings) {
  throw "The canonical Ghostfolio user has fewer than $MinimumHoldings holdings"
}
if ($MinimumHoldings -ge 3) {
  $missingSymbols = @($expectedHoldingSymbols | Where-Object { $_ -notin $canonicalIdentity.holdingSymbols })
  if ($missingSymbols.Count -gt 0) {
    throw "The canonical Ghostfolio user is missing expected holdings: $($missingSymbols -join ', ')"
  }
}

New-Item -ItemType Directory -Path $ghostfolioRoot -Force | Out-Null
icacls $ghostfolioRoot /inheritance:r /grant:r "${env:USERNAME}:(OI)(CI)F" | Out-Null
if ($LASTEXITCODE -ne 0) {
  throw 'Could not enforce the user-only Ghostfolio folder ACL'
}

$existingToken = if (Test-Path -LiteralPath $keyPath) { (Get-Content -LiteralPath $keyPath -Raw).Trim() } else { '' }
if ($existingToken -and $existingToken -cne $canonicalToken) {
  New-Item -ItemType Directory -Path $archiveDirectory -Force | Out-Null
  $archivePath = Join-Path $archiveDirectory ("empty-profile-key-{0}.txt" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
  [IO.File]::WriteAllText($archivePath, "$existingToken`r`n", [Text.UTF8Encoding]::new($false))
  icacls $archiveDirectory /inheritance:r /grant:r "${env:USERNAME}:(OI)(CI)F" | Out-Null
}

$temporaryKeyPath = "$keyPath.new"
[IO.File]::WriteAllText($temporaryKeyPath, "$canonicalToken`r`n", [Text.UTF8Encoding]::new($false))
icacls $temporaryKeyPath /inheritance:r /grant:r "${env:USERNAME}:F" | Out-Null
Move-Item -LiteralPath $temporaryKeyPath -Destination $keyPath -Force
icacls $keyPath /inheritance:r /grant:r "${env:USERNAME}:F" | Out-Null

$visibleIdentity = Get-GhostfolioIdentity ((Get-Content -LiteralPath $keyPath -Raw).Trim())
if (
  $visibleIdentity.id -ne $canonicalIdentity.id -or
  $visibleIdentity.holdingCount -ne $canonicalIdentity.holdingCount -or
  @(Compare-Object $visibleIdentity.holdingSymbols $canonicalIdentity.holdingSymbols).Count -ne 0
) {
  throw 'Key.txt does not authenticate the canonical synced Ghostfolio portfolio'
}

[pscustomobject]@{
  keyPath = $keyPath
  canonicalUserId = $canonicalIdentity.id
  holdingCount = $canonicalIdentity.holdingCount
  synchronized = $true
}
