param(
    [ValidateSet('http', 'stdio')][string]$Transport = 'http',
    [ValidateRange(1024, 65535)][int]$Port = 8765,
    [string]$Config = ''
)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) {
    throw 'Missing project environment. Run .\build.ps1 first.'
}
if (-not $Config) { $Config = Join-Path $ProjectRoot 'local-config\config.toml' }
& $Python -B -m local_files_mcp.server --config $Config --transport $Transport --port $Port
exit $LASTEXITCODE
