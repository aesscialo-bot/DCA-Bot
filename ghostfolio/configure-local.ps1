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
$byName = @{}
foreach ($account in $accounts) { $byName[$account.name] = $account.id }
if (-not $byName['Kraken DCA'] -or -not $byName['Bitkub Legacy']) {
  throw 'Required custody accounts were not returned by Ghostfolio'
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
Invoke-RestMethod -Method Put -Uri 'http://127.0.0.1:3333/api/v1/user/setting' -TimeoutSec 120 -Headers $headers -ContentType 'application/json' -Body '{"baseCurrency":"GBP"}' | Out-Null
$env:DCA_GHOSTFOLIO_SECRETS_FILE = $secretFile
docker compose -f (Join-Path $PSScriptRoot 'compose.yml') up -d --force-recreate sync | Out-Null
Write-Host 'Ghostfolio base currency is GBP; local custody account IDs and security token are stored under the user-only ACL.'
