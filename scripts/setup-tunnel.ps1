[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'build-support.ps1')
$architecture = Get-BuildArchitecture
$version = 'v0.0.15'
# Official immutable release asset digests:
# https://github.com/openai/tunnel-client/releases/expanded_assets/v0.0.15
$hashes = @{
    amd64 = '3b53133a1e24d43f63088d843860cb1701a4c3ed6390de2e19f69089e43bddc1'
    arm64 = '571e0d59ed9e86d1b105dc34f3267865f654de6968b01efd7c847f0af657d11d'
}
$assetName = "tunnel-client-$version-windows-$architecture.zip"
$downloadUrl = "https://github.com/openai/tunnel-client/releases/download/$version/$assetName"
$archive = Get-VerifiedBuildArchive $projectRoot $downloadUrl $assetName $hashes[$architecture]
$vendorPath = Join-Path $projectRoot 'vendor\tunnel-client'
Expand-BuildArchive $projectRoot $archive $vendorPath
if (-not (Test-Path -LiteralPath (Join-Path $vendorPath 'tunnel-client.exe') -PathType Leaf)) {
    throw 'The verified official archive does not contain tunnel-client.exe.'
}
Write-Host "Verified and installed official OpenAI tunnel-client $version ($architecture). No service was started."
