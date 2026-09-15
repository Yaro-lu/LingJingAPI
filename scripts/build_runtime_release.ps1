#requires -Version 7.2

[CmdletBinding()]
param(
    [string]$Version = "",
    [string]$SourceRoot = "",
    [string]$OutputRoot = "",
    [string]$PackageBaseName = "runtime-nvidia-rtx20plus-cu130",
    [string]$Repository = "Yaro-lu/LingJingAPI",
    [switch]$ValidateOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir
$ProjectVersionPath = Join-Path $ProjectDir "VERSION"
if (-not (Test-Path -LiteralPath $ProjectVersionPath -PathType Leaf)) {
    throw "Project VERSION file not found: $ProjectVersionPath"
}
$ProjectVersion = (Get-Content -LiteralPath $ProjectVersionPath -Raw).Trim()
if (-not $Version) {
    $Version = $ProjectVersion
}
elseif ($Version -ne $ProjectVersion) {
    throw "Requested runtime version $Version does not match project VERSION $ProjectVersion"
}
if ($Version -notmatch '^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$') {
    throw "Invalid runtime version: $Version"
}
$RuntimeSourceRoot = if ($SourceRoot) {
    [System.IO.Path]::GetFullPath($SourceRoot)
}
else {
    $ProjectDir
}
$DistDir = if ($OutputRoot) { [System.IO.Path]::GetFullPath($OutputRoot) } else { Join-Path $ProjectDir "dist" }
$PackageName = "$PackageBaseName-v$Version.7z"
$ArchivePath = Join-Path $DistDir $PackageName
$HashPath = "$ArchivePath.sha256"
$ReleaseManifestPath = "$ArchivePath.release.json"
$StagingBase = Join-Path $DistDir "runtime-staging"
$StageRoot = Join-Path $StagingBase "$PackageBaseName-v$Version"

function Write-Step {
    param([Parameter(Mandatory)][string]$Message)
    Write-Host "[Runtime] $Message" -ForegroundColor Cyan
}

function Get-FullPath {
    param([Parameter(Mandatory)][string]$Path)
    return [System.IO.Path]::GetFullPath($Path)
}

function Assert-SafeStagePath {
    $parentFull = (Get-FullPath $StagingBase).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    $stageFull = Get-FullPath $StageRoot
    $prefix = $parentFull + [System.IO.Path]::DirectorySeparatorChar
    if (-not $stageFull.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing unsafe runtime staging path: $stageFull"
    }
    if (-not (Split-Path -Leaf $stageFull).StartsWith(
        "runtime-nvidia-",
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Refusing unexpected runtime staging path: $stageFull"
    }
}

function Remove-SafeStage {
    Assert-SafeStagePath
    if (Test-Path -LiteralPath $StageRoot) {
        Remove-Item -LiteralPath $StageRoot -Recurse -Force
    }
}

function Test-ExcludedRelativePath {
    param(
        [Parameter(Mandatory)][string]$RelativePath,
        [Parameter(Mandatory)][ValidateSet("Venv", "RuntimePython", "ComfyUI")]
        [string]$Profile
    )

    $normal = $RelativePath.Replace('/', '\').TrimStart('\')
    $segments = @($normal.Split('\', [System.StringSplitOptions]::RemoveEmptyEntries))
    $leaf = if ($segments.Count) { $segments[-1] } else { "" }

    if ($segments | Where-Object { $_ -in @('.git', '.gitnexus', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache') }) {
        return $true
    }
    if ($leaf -match '(?i)\.(pyc|pyo)$') {
        return $true
    }
    if ($leaf -match '(?i)^\.env(?:\..*)?$|\.(key|token|pfx|p12)$' -or
        $leaf -in @('extra_model_paths.yaml', '.lingjing-model-mappings.json', 'config.local.txt')) {
        return $true
    }
    if ($leaf -match '(?i)^(session|account_session|client_instance|workflow_config|config\.local)\.json$') {
        return $true
    }
    if ($leaf -match '(?i)^\.(session|workflow_config)\.lock$' -or $leaf -match '(?i)^gateway\.lock$') {
        return $true
    }

    $first = if ($segments.Count) { $segments[0].ToLowerInvariant() } else { "" }
    if ($Profile -eq 'RuntimePython' -and $first -in @(
        'models', 'inputs', 'outputs', 'logs', 'tasks', 'temp', 'cache', 'scripts'
    )) {
        return $true
    }
    if ($Profile -eq 'ComfyUI' -and $first -in @(
        'models', 'input', 'inputs', 'output', 'outputs', 'temp', 'user',
        'logs', 'tasks', 'cache'
    )) {
        return $true
    }
    return $false
}

function Copy-SanitizedTree {
    param(
        [Parameter(Mandatory)][string]$Source,
        [Parameter(Mandatory)][string]$Destination,
        [Parameter(Mandatory)][ValidateSet("Venv", "RuntimePython", "ComfyUI")]
        [string]$Profile
    )

    if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
        throw "Required runtime source directory is missing: $Source"
    }
    $sourceFull = (Get-FullPath $Source).TrimEnd('\')
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null

    foreach ($item in Get-ChildItem -LiteralPath $sourceFull -Recurse -Force) {
        $relative = $item.FullName.Substring($sourceFull.Length).TrimStart('\')
        if (Test-ExcludedRelativePath -RelativePath $relative -Profile $Profile) {
            continue
        }
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Reparse points are not allowed in runtime input: $($item.FullName)"
        }

        $target = Join-Path $Destination $relative
        if ($item.PSIsContainer) {
            New-Item -ItemType Directory -Path $target -Force | Out-Null
        }
        else {
            New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
            Copy-Item -LiteralPath $item.FullName -Destination $target -Force
        }
    }
}

function Copy-RequiredFile {
    param(
        [Parameter(Mandatory)][string]$Source,
        [Parameter(Mandatory)][string]$Destination
    )

    if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) {
        throw "Required runtime file is missing: $Source"
    }
    if ((Get-Item -LiteralPath $Source -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
        throw "Reparse points are not allowed in runtime input: $Source"
    }
    New-Item -ItemType Directory -Path (Split-Path -Parent $Destination) -Force | Out-Null
    Copy-Item -LiteralPath $Source -Destination $Destination -Force
}

function Resolve-ArchiveTool {
    foreach ($name in @('7z.exe', '7zz.exe', '7za.exe')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command) {
            return [pscustomobject]@{ Kind = '7z'; Path = $command.Source }
        }
    }
    foreach ($candidate in @(
        (Join-Path $env:ProgramFiles '7-Zip\7z.exe'),
        (Join-Path ${env:ProgramFiles(x86)} '7-Zip\7z.exe')
    )) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return [pscustomobject]@{ Kind = '7z'; Path = $candidate }
        }
    }
    foreach ($name in @('tar.exe', 'bsdtar.exe', 'bsdtar')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command) {
            return [pscustomobject]@{ Kind = 'tar'; Path = $command.Source }
        }
    }
    throw "7-Zip or Windows bsdtar/tar.exe is required to create the runtime package."
}

function Normalize-MemberName {
    param([Parameter(Mandatory)][string]$Name)

    $member = $Name.Trim().Replace('\', '/')
    while ($member.StartsWith('./', [System.StringComparison]::Ordinal)) {
        $member = $member.Substring(2)
    }
    return $member.TrimEnd('/')
}

function Get-ArchiveMembers {
    param(
        [Parameter(Mandatory)]$Tool,
        [Parameter(Mandatory)][string]$Archive
    )

    if ($Tool.Kind -eq '7z') {
        $raw = @(& $Tool.Path l -slt $Archive 2>&1)
        $exitCode = $LASTEXITCODE
        if ($exitCode -ne 0) {
            throw "7-Zip could not list the generated archive (exit $exitCode): $($raw -join [Environment]::NewLine)"
        }

        $archiveFull = (Get-FullPath $Archive).Replace('\', '/')
        $archiveLeaf = Split-Path -Leaf $Archive
        $members = @(
            foreach ($line in $raw) {
                $text = "$line"
                if ($text -match '^Path = (.+)$') {
                    $candidate = Normalize-MemberName $Matches[1]
                    if (
                        $candidate -and
                        -not $candidate.Equals($archiveFull, [System.StringComparison]::OrdinalIgnoreCase) -and
                        -not $candidate.Equals($archiveLeaf, [System.StringComparison]::OrdinalIgnoreCase) -and
                        -not $candidate.EndsWith(
                            "/$archiveLeaf",
                            [System.StringComparison]::OrdinalIgnoreCase
                        )
                    ) {
                        $candidate
                    }
                }
            }
        )
    }
    else {
        $raw = @(& $Tool.Path -tf $Archive 2>&1)
        $exitCode = $LASTEXITCODE
        if ($exitCode -ne 0) {
            throw "tar could not list the generated archive (exit $exitCode): $($raw -join [Environment]::NewLine)"
        }
        $members = @($raw | ForEach-Object { Normalize-MemberName "$_" } | Where-Object { $_ })
    }

    return @($members | Sort-Object -Unique)
}

function Assert-ArchivePolicy {
    param([Parameter(Mandatory)][string[]]$Members)

    $requiredMembers = @(
        'runtime/python/python.exe',
        'runtime/ComfyUI/main.py',
        '.venv/Lib/site-packages/torch/__init__.py',
        'bin/cloudflared.exe'
    )
    $memberSet = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::OrdinalIgnoreCase
    )
    foreach ($member in $Members) {
        [void]$memberSet.Add($member)
    }
    $missing = @($requiredMembers | Where-Object { -not $memberSet.Contains($_) })
    if ($missing.Count) {
        throw "Generated archive is incomplete. Missing: $($missing -join ', ')"
    }

    $allowedRootPatterns = @(
        '^\.venv$',
        '^\.venv/Lib(?:/|$)',
        '^\.venv/share(?:/|$)',
        '^runtime$',
        '^runtime/python(?:/|$)',
        '^runtime/ComfyUI(?:/|$)',
        '^bin(?:/|$)'
    )
    $forbiddenPatterns = @(
        '(^|/)(\.git|\.gitnexus|__pycache__)(/|$)',
        '\.(pyc|pyo)$',
        '^runtime/ComfyUI/(models|input|inputs|output|outputs|temp|user|logs|tasks|cache)(/|$)',
        '(^|/)(session|account_session|client_instance|workflow_config|config\.local)\.json$',
        '(^|/)\.(session|workflow_config)\.lock$',
        '(^|/)gateway\.lock$'
    )

    $violations = [System.Collections.Generic.List[string]]::new()
    foreach ($member in $Members) {
        $allowed = $false
        foreach ($pattern in $allowedRootPatterns) {
            if ($member -match $pattern) {
                $allowed = $true
                break
            }
        }
        if (-not $allowed) {
            $violations.Add($member)
            continue
        }
        foreach ($pattern in $forbiddenPatterns) {
            if ($member -match $pattern) {
                $violations.Add($member)
                break
            }
        }
        if ($member -match '^bin/' -and -not $member.Equals(
            'bin/cloudflared.exe',
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
            $violations.Add($member)
        }
    }

    if ($violations.Count) {
        $sample = @($violations | Sort-Object -Unique | Select-Object -First 10)
        throw "Generated archive contains forbidden members: $($sample -join ', ')"
    }
}

if ($Version -notmatch '^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$') {
    throw "Version is not a safe semantic version: $Version"
}
if ($PackageBaseName -notmatch '^runtime-nvidia-[a-z0-9][a-z0-9.-]*$') {
    throw "Package base name is not safe: $PackageBaseName"
}
if ($Repository -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$') {
    throw "GitHub repository must use owner/name form: $Repository"
}
if (-not (Test-Path -LiteralPath $RuntimeSourceRoot -PathType Container)) {
    throw "Runtime source root does not exist: $RuntimeSourceRoot"
}

$requiredInputs = @(
    'runtime\python\python.exe',
    'runtime\ComfyUI\main.py',
    '.venv\Lib\site-packages\torch\__init__.py',
    'bin\cloudflared.exe'
)
$missingInputs = @($requiredInputs | Where-Object {
    -not (Test-Path -LiteralPath (Join-Path $RuntimeSourceRoot $_) -PathType Leaf)
})
if ($missingInputs.Count) {
    throw "Environment is incomplete. Missing: $($missingInputs -join ', ')"
}
if (-not (Test-Path -LiteralPath (Join-Path $RuntimeSourceRoot '.venv\Lib') -PathType Container)) {
    throw "Environment is incomplete. Missing: .venv\Lib"
}

Write-Host "[Runtime] Package source contract is valid." -ForegroundColor Green
Write-Host "[Runtime] Source root: $RuntimeSourceRoot"
Write-Host "[Runtime] Only .venv/Lib, optional .venv/share, portable Python, sanitized ComfyUI, and Cloudflared are allowed."
if ($ValidateOnly) {
    exit 0
}

$tool = Resolve-ArchiveTool
New-Item -ItemType Directory -Path $DistDir -Force | Out-Null
New-Item -ItemType Directory -Path $StagingBase -Force | Out-Null
Remove-SafeStage
New-Item -ItemType Directory -Path $StageRoot -Force | Out-Null
Remove-Item -LiteralPath $ArchivePath -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $HashPath -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $ReleaseManifestPath -Force -ErrorAction SilentlyContinue

$buildSucceeded = $false
try {
    Write-Step "Creating sanitized runtime staging"
    Copy-SanitizedTree -Source (Join-Path $RuntimeSourceRoot '.venv\Lib') -Destination (Join-Path $StageRoot '.venv\Lib') -Profile Venv
    $venvShare = Join-Path $RuntimeSourceRoot '.venv\share'
    if (Test-Path -LiteralPath $venvShare -PathType Container) {
        Copy-SanitizedTree -Source $venvShare -Destination (Join-Path $StageRoot '.venv\share') -Profile Venv
    }
    Copy-SanitizedTree -Source (Join-Path $RuntimeSourceRoot 'runtime\python') -Destination (Join-Path $StageRoot 'runtime\python') -Profile RuntimePython
    Copy-SanitizedTree -Source (Join-Path $RuntimeSourceRoot 'runtime\ComfyUI') -Destination (Join-Path $StageRoot 'runtime\ComfyUI') -Profile ComfyUI
    Copy-RequiredFile -Source (Join-Path $RuntimeSourceRoot 'bin\cloudflared.exe') -Destination (Join-Path $StageRoot 'bin\cloudflared.exe')

    Write-Step "Removing superseded dependency copies from staging"
    & (Join-Path $RuntimeSourceRoot 'runtime\python\python.exe') -s -B `
        (Join-Path $ScriptDir 'prune_shadowed_runtime.py') --stage $StageRoot `
        --report (Join-Path $DistDir 'runtime-dedup-report.json')
    if ($LASTEXITCODE -ne 0) { throw "Runtime dependency deduplication failed" }

    Write-Step "Creating $PackageName with $($tool.Kind)"
    Push-Location $StageRoot
    try {
        if ($tool.Kind -eq '7z') {
            & $tool.Path a -t7z -m0=lzma2 -mx=9 -mfb=64 -md=256m -mmt=on -ms=on $ArchivePath '.venv' 'runtime' 'bin'
        }
        else {
            & $tool.Path -a -cf $ArchivePath '.venv' 'runtime' 'bin'
        }
        if ($LASTEXITCODE -ne 0) {
            throw "Archive creation failed with exit code $LASTEXITCODE"
        }
    }
    finally {
        Pop-Location
    }

    Write-Step "Enumerating and validating archive members"
    $members = Get-ArchiveMembers -Tool $tool -Archive $ArchivePath
    if (-not $members.Count) {
        throw "The generated archive contains no members"
    }
    Assert-ArchivePolicy -Members $members

    $archive = Get-Item -LiteralPath $ArchivePath
    if ($archive.Length -ge 2GB) {
        throw "Release asset is $([Math]::Round($archive.Length / 1MB, 0)) MiB; GitHub requires each asset to be below 2 GiB."
    }

    $hash = (Get-FileHash -LiteralPath $ArchivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    "$hash  $PackageName" | Set-Content -LiteralPath $HashPath -Encoding ascii
    $releaseTag = "v$Version"
    $releaseUrl = "https://github.com/$Repository/releases/download/$releaseTag/$PackageName"
    [ordered]@{
        schema_version = 1
        version = $Version
        release_tag = $releaseTag
        package_name = $PackageName
        size_bytes = (Get-Item -LiteralPath $ArchivePath).Length
        sha256 = $hash
        download_url = $releaseUrl
        homepage_url = "https://github.com/$Repository"
    } | ConvertTo-Json | Set-Content -LiteralPath $ReleaseManifestPath -Encoding utf8
    $buildSucceeded = $true

    Write-Host "[Runtime] Created: $ArchivePath" -ForegroundColor Green
    Write-Host "[Runtime] Release manifest: $ReleaseManifestPath"
    Write-Host "[Runtime] Members: $($members.Count)"
    Write-Host "[Runtime] Size: $([Math]::Round($archive.Length / 1GB, 3)) GiB"
    Write-Host "[Runtime] SHA256: $hash"
}
finally {
    Remove-SafeStage
    if (-not $buildSucceeded) {
        Remove-Item -LiteralPath $ArchivePath -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $HashPath -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $ReleaseManifestPath -Force -ErrorAction SilentlyContinue
    }
}
