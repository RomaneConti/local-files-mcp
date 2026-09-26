[CmdletBinding()]
param(
    [ValidatePattern('^tunnel_[A-Za-z0-9_-]+$')]
    [string]$TunnelId,
    [switch]$CheckOnly,
    [string]$DataDir
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw 'Missing project environment. Run .\build.ps1 first.'
}
$tunnelArguments = @('-B', '-m', 'local_files_mcp.launcher_config', '--bundle-dir', $projectRoot)
if ($DataDir) { $tunnelArguments += @('--data-dir', $DataDir) }
if ($TunnelId) { $tunnelArguments += @('--tunnel-id', $TunnelId) }
if ($CheckOnly) { $tunnelArguments += '--check-only' }
& $pythonPath @tunnelArguments
exit $LASTEXITCODE
