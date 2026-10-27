param(
    [string]$CodexHome = $(if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' })
)

$ErrorActionPreference = 'Stop'
$marketplaceRoot = Join-Path $CodexHome 'local-marketplaces/p1nbored-local'
$marketplaceFile = Join-Path $marketplaceRoot '.agents/plugins/marketplace.json'
$pluginDestination = Join-Path $marketplaceRoot 'plugins/wechat-cli'
$utf8 = [System.Text.UTF8Encoding]::new($false)

if (Test-Path -LiteralPath $marketplaceFile) {
    $marketplace = Get-Content -LiteralPath $marketplaceFile -Raw | ConvertFrom-Json
    if ($marketplace.name -ne 'p1nbored-local') { throw 'Unexpected marketplace identity.' }
} else {
    $marketplace = [pscustomobject]@{ name = 'p1nbored-local'; plugins = @() }
}

New-Item -ItemType Directory -Force -Path $pluginDestination | Out-Null
foreach ($name in @('plugin.json', '.codex-plugin', 'skills', 'README.md')) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination $pluginDestination -Recurse -Force
}

$entry = [pscustomobject]@{
    name = 'wechat-cli'
    source = @{ source = 'local'; path = './plugins/wechat-cli' }
    policy = @{ installation = 'AVAILABLE'; authentication = 'ON_USE' }
    category = 'Productivity'
}
$marketplace.plugins = @($marketplace.plugins | Where-Object { $_.name -ne 'wechat-cli' }) + @($entry)
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $marketplaceFile) | Out-Null
[System.IO.File]::WriteAllText($marketplaceFile, ($marketplace | ConvertTo-Json -Depth 20) + "`n", $utf8)

# Both commands use Codex's supported registration/cache flow.
$previousCodexHome = $env:CODEX_HOME
try {
    $env:CODEX_HOME = $CodexHome
    & codex plugin marketplace add $marketplaceRoot
    if ($LASTEXITCODE -ne 0) { throw 'Marketplace registration failed.' }
    & codex plugin add 'wechat-cli@p1nbored-local' --json
    if ($LASTEXITCODE -ne 0) { throw 'Plugin installation failed.' }
} finally {
    $env:CODEX_HOME = $previousCodexHome
}
