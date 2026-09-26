# Helpers shared by the Windows build and pinned tunnel-client download.
# Downloads use HTTPS and environment/system proxy settings.
function Assert-BuildPath {
    param([string]$ProjectRoot, [string]$Path)
    $root = [IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\')
    $full = [IO.Path]::GetFullPath($Path)
    if (-not $full.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Build output must stay inside the project directory.'
    }
    # GetFullPath alone does not resolve junctions. Inspect every ancestor.
    $current = $full
    while ($current) {
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -Force -LiteralPath $current
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw 'Build output has a reparse-point path or ancestor.'
            }
        }
        $parent = [IO.Path]::GetDirectoryName($current)
        if ($parent -eq $current) { break }
        $current = $parent
    }
    return $full
}

function Assert-BuildTree {
    param([string]$ProjectRoot, [string]$Path)
    $full = Assert-BuildPath $ProjectRoot $Path
    if (-not (Test-Path -LiteralPath $full)) { return }
    $pending = New-Object 'System.Collections.Generic.Stack[string]'
    $pending.Push($full)
    while ($pending.Count) {
        $current = $pending.Pop()
        $item = Get-Item -Force -LiteralPath $current
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'Build output contains a reparse point; refusing to replace its tree.'
        }
        if ($item.PSIsContainer) {
            foreach ($child in Get-ChildItem -Force -LiteralPath $current) { $pending.Push($child.FullName) }
        }
    }
}

function Get-BuildArchitecture {
    $architecture = $env:PROCESSOR_ARCHITEW6432
    if (-not $architecture) { $architecture = $env:PROCESSOR_ARCHITECTURE }
    $architecture = $architecture.ToLowerInvariant()
    if ($architecture -notin @('amd64', 'arm64')) { throw 'This build requires Windows x64 or ARM64.' }
    return $architecture
}

function Remove-PinnedPythonAlias {
    param([string]$ProjectRoot, [string]$PythonVersion, [string]$Architecture)
    # uv creates one minor-version junction beside its real pinned interpreter.
    # Remove only that known alias before the general no-reparse tree check.
    $machines = @{ amd64 = 'x86_64'; arm64 = 'aarch64' }
    if ($PythonVersion -notmatch '^\d+\.\d+\.\d+$' -or -not $machines.ContainsKey($Architecture)) {
        throw 'Invalid pinned Python alias metadata.'
    }
    $folder = Assert-BuildPath $ProjectRoot (Join-Path $ProjectRoot '.cache\python')
    $minorVersion = ($PythonVersion.Split('.')[0..1] -join '.')
    $suffix = "windows-$($machines[$Architecture])-none"
    $alias = Join-Path $folder "cpython-$minorVersion-$suffix"
    $item = Get-Item -Force -LiteralPath $alias -ErrorAction SilentlyContinue
    if (-not $item -or -not ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { return }
    $expected = Assert-BuildPath $ProjectRoot (Join-Path $folder "cpython-$PythonVersion-$suffix")
    $targets = @($item.Target)
    if ($item.LinkType -ne 'Junction' -or $targets.Count -ne 1 -or
        -not [IO.Path]::IsPathRooted([string]$targets[0]) -or
        -not [IO.Path]::GetFullPath([string]$targets[0]).Equals($expected, [StringComparison]::OrdinalIgnoreCase) -or
        -not (Test-Path -LiteralPath $expected -PathType Container)) {
        throw 'Managed Python alias does not point to the expected pinned interpreter; refusing to remove it.'
    }
    # Non-recursive Directory.Delete removes the junction itself, never its target.
    [IO.Directory]::Delete($alias)
}

function Get-BuildProxy {
    foreach ($name in @('HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY')) {
        $value = [Environment]::GetEnvironmentVariable($name)
        if ($value) { return $value }
    }
    $target = [Uri]'https://github.com/'
    $proxy = [Net.WebRequest]::GetSystemWebProxy().GetProxy($target)
    if ($proxy -and $proxy.AbsoluteUri -ne $target.AbsoluteUri) { return $proxy.AbsoluteUri }
    return $null
}

function Get-BuildSha256 {
    param([string]$Path)
    # Do not depend on Get-FileHash module autoloading in Windows PowerShell 5.1.
    $stream = [IO.File]::OpenRead($Path)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose(); $stream.Dispose() }
}

function Get-VerifiedBuildArchive {
    param([string]$ProjectRoot, [string]$Url, [string]$Name, [string]$Sha256)
    if (([Uri]$Url).Scheme -ne 'https') { throw 'Build downloads require HTTPS.' }
    if ($Name -ne [IO.Path]::GetFileName($Name) -or $Sha256 -notmatch '^[a-f0-9]{64}$') {
        throw 'Invalid pinned download metadata.'
    }
    $folder = Assert-BuildPath $ProjectRoot (Join-Path $ProjectRoot '.cache\downloads')
    New-Item -ItemType Directory -Force -Path $folder | Out-Null
    $archive = Assert-BuildPath $ProjectRoot (Join-Path $folder $Name)
    if ((Test-Path -LiteralPath $archive -PathType Leaf) -and
        (Get-BuildSha256 $archive) -eq $Sha256) {
        return $archive
    }
    $temporary = Assert-BuildPath $ProjectRoot ($archive + '.download')
    $previousTls = [Net.ServicePointManager]::SecurityProtocol
    try {
        [Net.ServicePointManager]::SecurityProtocol = $previousTls -bor [Net.SecurityProtocolType]::Tls12
        $request = @{ Uri = $Url; OutFile = $temporary; UseBasicParsing = $true; ErrorAction = 'Stop' }
        $proxy = Get-BuildProxy
        if ($proxy) { $request.Proxy = $proxy }
        Write-Host "Downloading pinned $Name"
        try { Invoke-WebRequest @request } catch {
            throw 'Build download failed. Check internet access, system proxy or HTTPS_PROXY, then run build.ps1 again.'
        }
        if ((Get-BuildSha256 $temporary) -ne $Sha256) {
            throw 'Pinned download SHA-256 mismatch; nothing was extracted.'
        }
        Move-Item -LiteralPath $temporary -Destination $archive -Force
        return $archive
    }
    finally {
        [Net.ServicePointManager]::SecurityProtocol = $previousTls
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}

function Expand-BuildArchive {
    param([string]$ProjectRoot, [string]$Archive, [string]$Destination)
    Assert-BuildTree $ProjectRoot $Destination
    $destinationPath = Assert-BuildPath $ProjectRoot $Destination
    [void][Reflection.Assembly]::Load('System.IO.Compression.FileSystem, Version=4.0.0.0, Culture=neutral, PublicKeyToken=b77a5c561934e089')
    $zip = [IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        foreach ($entry in $zip.Entries) {
            $name = $entry.FullName.Replace('/', '\')
            if ([IO.Path]::IsPathRooted($name) -or $name.Split('\') -contains '..' -or $name.Contains(':')) {
                throw 'Unsafe path in official archive; extraction refused.'
            }
            $target = [IO.Path]::GetFullPath((Join-Path $destinationPath $name))
            if (-not $target.StartsWith($destinationPath.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
                throw 'Archive entry is outside its destination.'
            }
        }
        # Extract with the same .NET archive API, without Expand-Archive module
        # autoloading. All destinations were validated before any file is written.
        [void][IO.Directory]::CreateDirectory($destinationPath)
        foreach ($entry in $zip.Entries) {
            $target = Join-Path $destinationPath $entry.FullName.Replace('/', '\')
            if (-not $entry.Name) {
                [void][IO.Directory]::CreateDirectory($target)
            } else {
                [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($target))
                [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target, $true)
            }
        }
    }
    finally { $zip.Dispose() }
}
