$ErrorActionPreference = 'Stop'
$secretFile = Join-Path $env:LOCALAPPDATA 'dca-ghostfolio\secrets.env'
$securityToken = (Get-Clipboard -Raw).Trim()
if ($securityToken -notmatch '^[0-9a-f]{128}$') {
  throw 'Clipboard does not contain the expected 128-character Ghostfolio security token'
}

$auth = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:3333/api/v1/auth/anonymous' -TimeoutSec 15 -ContentType 'application/json' -Body (@{ accessToken = $securityToken } | ConvertTo-Json -Compress)
if (-not $auth.authToken) { throw 'Ghostfolio did not return an authentication token' }
$headers = @{ Authorization = "Bearer $($auth.authToken)" }
$accountsResponse = Invoke-RestMethod -Method Get -Uri 'http://127.0.0.1:3333/api/v1/account' -TimeoutSec 15 -Headers $headers
$accounts = if ($accountsResponse.accounts) { $accountsResponse.accounts } else { $accountsResponse }
$requiredAccountNames = @('Kraken DCA', 'Bitkub Legacy')
foreach ($account in $accounts | Where-Object { $_.name -in $requiredAccountNames }) {
  if ($account.currency -ne 'GBP') {
    $body = @{
      balance = [double]$account.balance
      currency = 'GBP'
      id = $account.id
      name = $account.name
      platformId = $account.platformId
    } | ConvertTo-Json -Compress
    $account = Invoke-RestMethod -Method Put -Uri "http://127.0.0.1:3333/api/v1/account/$($account.id)" -TimeoutSec 30 -Headers $headers -ContentType 'application/json' -Body $body
  }
}
$accountsResponse = Invoke-RestMethod -Method Get -Uri 'http://127.0.0.1:3333/api/v1/account' -TimeoutSec 15 -Headers $headers
$accounts = if ($accountsResponse.accounts) { $accountsResponse.accounts } else { $accountsResponse }
$byName = @{}
foreach ($account in $accounts) { $byName[$account.name] = $account.id }
if (-not $byName['Kraken DCA'] -or -not $byName['Bitkub Legacy']) {
  throw 'Required custody accounts were not returned by Ghostfolio'
}
if ($byName['Kraken DCA'] -eq $byName['Bitkub Legacy']) {
  throw 'Kraken DCA and Bitkub Legacy must have distinct account IDs'
}
foreach ($requiredName in $requiredAccountNames) {
  $matches = @($accounts | Where-Object { $_.name -eq $requiredName })
  if ($matches.Count -ne 1) { throw "Ghostfolio requires exactly one $requiredName account" }
  $account = $matches[0]
  if ($account.currency -ne 'GBP') { throw "$requiredName must use GBP as its account currency" }
}
$accountMap = @{
  BTC_GBP = $byName['Kraken DCA']
  HYPE_USD = $byName['Kraken DCA']
  SOL_GBP = $byName['Kraken DCA']
  BITKUB_LEGACY = $byName['Bitkub Legacy']
} | ConvertTo-Json -Compress

$content = [IO.File]::ReadAllText($secretFile)
$content = [regex]::Replace($content, '(?m)^GHOSTFOLIO_SECURITY_TOKEN=.*$', "GHOSTFOLIO_SECURITY_TOKEN=$securityToken")
$content = [regex]::Replace($content, '(?m)^GHOSTFOLIO_ACCOUNT_MAP=.*$', "GHOSTFOLIO_ACCOUNT_MAP=$accountMap")
[IO.File]::WriteAllText($secretFile, $content, [Text.UTF8Encoding]::new($false))
icacls (Split-Path $secretFile) /inheritance:r /grant:r "${env:USERNAME}:(OI)(CI)F" | Out-Null
& (Join-Path $PSScriptRoot 'write-service-env.ps1') | Out-Null
& (Join-Path $PSScriptRoot 'sync-canonical-key.ps1') -MinimumHoldings 0 | Out-Null
Invoke-RestMethod -Method Put -Uri 'http://127.0.0.1:3333/api/v1/user/setting' -TimeoutSec 120 -Headers $headers -ContentType 'application/json' -Body '{"baseCurrency":"GBP"}' | Out-Null
$admin = Invoke-RestMethod -Method Get -Uri 'http://127.0.0.1:3333/api/v1/admin' -TimeoutSec 120 -Headers $headers
$currencies = $admin.settings.CURRENCIES
if ($currencies -is [string]) { $currencies = $currencies | ConvertFrom-Json }
$currencies = @($currencies)
foreach ($requiredCurrency in @('GBP', 'USD')) {
  if ($requiredCurrency -notin $currencies) { $currencies += $requiredCurrency }
}
$currencyValue = $currencies | ConvertTo-Json -Compress
Invoke-RestMethod -Method Put -Uri 'http://127.0.0.1:3333/api/v1/admin/settings/CURRENCIES' -TimeoutSec 120 -Headers $headers -ContentType 'application/json' -Body (@{ value = $currencyValue } | ConvertTo-Json -Compress) | Out-Null
$admin = Invoke-RestMethod -Method Get -Uri 'http://127.0.0.1:3333/api/v1/admin' -TimeoutSec 120 -Headers $headers
$verifiedCurrencies = $admin.settings.CURRENCIES
if ($verifiedCurrencies -is [string]) { $verifiedCurrencies = $verifiedCurrencies | ConvertFrom-Json }
if ('GBP' -notin @($verifiedCurrencies) -or 'USD' -notin @($verifiedCurrencies)) {
  throw 'Ghostfolio did not retain the required GBP and USD currencies'
}
$env:DCA_GHOSTFOLIO_SECRETS_FILE = $secretFile
docker compose -f (Join-Path $PSScriptRoot 'compose.yml') stop sync | Out-Null
docker compose -f (Join-Path $PSScriptRoot 'compose.yml') up -d --force-recreate postgres redis app | Out-Null
$deadline = (Get-Date).AddSeconds(90)
do {
  $appHealth = docker inspect --format '{{.State.Health.Status}}' dca-ghostfolio-app-1
  if ($appHealth -eq 'healthy') { break }
  Start-Sleep -Seconds 2
} while ((Get-Date) -lt $deadline)
if ($appHealth -ne 'healthy') { throw "Ghostfolio app did not become healthy: $appHealth" }
docker compose -f (Join-Path $PSScriptRoot 'compose.yml') build sync | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Ghostfolio reporting sidecar build failed' }
docker compose -f (Join-Path $PSScriptRoot 'compose.yml') run --rm --no-deps sync once | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Ghostfolio initial reporting sync failed' }
docker compose -f (Join-Path $PSScriptRoot 'compose.yml') up -d --build --force-recreate sync | Out-Null
Write-Host 'Ghostfolio base currency, custody accounts, and USD/GBP reporting bridge are configured for GBP; local account IDs and security token are stored under the user-only ACL.'
