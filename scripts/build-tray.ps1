[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'build-support.ps1')
$architecture = Get-BuildArchitecture
$uvVersion = '0.12.19'
$pythonVersion = '3.14.6'
$uvTargets = @{ amd64 = 'x86_64-pc-windows-msvc'; arm64 = 'aarch64-pc-windows-msvc' }
# Official release .sha256 files, pinned with the release.
# https://github.com/astral-sh/uv/releases/tag/0.12.19
$uvHashes = @{
    amd64 = '6dbb02d79e419522f1c500f0adb1cddcff0cda7d59b0d66ea7f5e3b4a1b2f5f0'
    arm64 = '115b54cb823bc48260670f5782001add6067ac8d98d18c8263a833704e287de9'
}
$uvFolder = Assert-BuildPath $projectRoot (Join-Path $projectRoot ".cache\tools\uv-$uvVersion-$architecture")
$uvPath = Join-Path $uvFolder 'uv.exe'
$assetName = "uv-$($uvTargets[$architecture]).zip"
$downloadUrl = "https://releases.astral.sh/github/uv/releases/download/$uvVersion/$assetName"
$archive = Get-VerifiedBuildArchive $projectRoot $downloadUrl "uv-$uvVersion-$assetName" $uvHashes[$architecture]
Expand-BuildArchive $projectRoot $archive $uvFolder
if (-not (Test-Path -LiteralPath $uvPath -PathType Leaf)) { throw 'The pinned uv archive did not contain uv.exe.' }

# Keep interpreter/cache/venv and subprocess temporary files in this checkout.
# No PATH or registry changes.
$settings = @{
    TEMP = (Join-Path $projectRoot '.cache\tmp')
    TMP = (Join-Path $projectRoot '.cache\tmp')
    UV_CACHE_DIR = (Join-Path $projectRoot '.cache\uv')
    UV_PYTHON_INSTALL_DIR = (Join-Path $projectRoot '.cache\python')
    UV_PYTHON_CACHE_DIR = (Join-Path $projectRoot '.cache\python-downloads')
    UV_PROJECT_ENVIRONMENT = (Join-Path $projectRoot '.venv')
    UV_PYTHON_INSTALL_BIN = '0'
    UV_PYTHON_INSTALL_REGISTRY = '0'
    UV_NO_MODIFY_PATH = '1'
    UV_NO_CONFIG = '1'
    UV_SYSTEM_CERTS = '1'
    UV_NO_PROGRESS = '1'
    PYTHONNOUSERSITE = '1'
    PYTHONDONTWRITEBYTECODE = '1'
    PYINSTALLER_CONFIG_DIR = (Join-Path $projectRoot '.cache\pyinstaller')
}
$savedEnvironment = @{}
foreach ($item in Get-ChildItem Env:) {
    if ($item.Name -like 'UV_*' -or $item.Name -like 'PYTHON*' -or
        $item.Name -in @('VIRTUAL_ENV', 'PYINSTALLER_CONFIG_DIR', 'TEMP', 'TMP')) {
        $savedEnvironment[$item.Name] = $item.Value
        [Environment]::SetEnvironmentVariable($item.Name, $null, 'Process')
    }
}
$proxy = Get-BuildProxy
$oldHttpsProxy = [Environment]::GetEnvironmentVariable('HTTPS_PROXY', 'Process')
if (-not $oldHttpsProxy -and $proxy) { [Environment]::SetEnvironmentVariable('HTTPS_PROXY', $proxy, 'Process') }
Push-Location $projectRoot
try {
    foreach ($name in $settings.Keys) { [Environment]::SetEnvironmentVariable($name, $settings[$name], 'Process') }
    Remove-PinnedPythonAlias $projectRoot $pythonVersion $architecture
    foreach ($relative in @('.venv', '.cache\python', '.cache\uv', '.cache\python-downloads',
                             '.cache\pyinstaller', '.cache\tmp', 'build', 'dist\LocalFilesMCP')) {
        Assert-BuildTree $projectRoot (Join-Path $projectRoot $relative)
    }
    New-Item -ItemType Directory -Force -Path $settings.TEMP | Out-Null
    & $uvPath python install $pythonVersion --no-bin --no-registry --no-progress
    if ($LASTEXITCODE -ne 0) { throw 'Could not install pinned managed Python. Check network/proxy access and retry build.ps1.' }
    & $uvPath sync --locked --group build --python $pythonVersion --managed-python --no-progress
    if ($LASTEXITCODE -ne 0) { throw 'Locked dependency installation failed; no lockfile update was attempted.' }
    & (Join-Path $PSScriptRoot 'setup-tunnel.ps1')
    $python = Join-Path $projectRoot '.venv\Scripts\python.exe'
    & $python -B (Join-Path $PSScriptRoot 'build-icon.py')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the tray icon.' }
    # PyInstaller recursively replaces its named output. Inspect parents and
    # existing descendants immediately before allowing that replacement.
    Assert-BuildTree $projectRoot (Join-Path $projectRoot 'dist\LocalFilesMCP')
    Assert-BuildTree $projectRoot (Join-Path $projectRoot 'build')
    & $python -B -m PyInstaller --noconfirm --distpath dist --workpath build LocalFilesMCP.spec
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
    Write-Host "Built: $(Join-Path $projectRoot 'dist\LocalFilesMCP\LocalFilesMCP.exe')"
    Write-Host 'Copy the entire LocalFilesMCP folder to another Windows computer. No service was started.'
}
finally {
    Pop-Location
    foreach ($item in Get-ChildItem Env:) {
        if ($item.Name -like 'UV_*' -or $item.Name -like 'PYTHON*' -or
            $item.Name -in @('VIRTUAL_ENV', 'PYINSTALLER_CONFIG_DIR', 'TEMP', 'TMP')) {
            [Environment]::SetEnvironmentVariable($item.Name, $null, 'Process')
        }
    }
    foreach ($name in $savedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], 'Process')
    }
    [Environment]::SetEnvironmentVariable('HTTPS_PROXY', $oldHttpsProxy, 'Process')
}
