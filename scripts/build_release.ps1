#requires -Version 7.2

<#
.SYNOPSIS
Builds a sanitized lightweight Windows client that installs the AI runtime separately.

.DESCRIPTION
The release contains the application, workflows, a pruned portable Python/Tk
bootstrap and a bundled 7-Zip extractor. ComfyUI, Torch, CUDA, Cloudflared,
models and user data remain in the separately distributed runtime package.
Inno Setup reads only the audited staging directory, never the live tree.
#>

[CmdletBinding()]
param(
    [string]$Version = "",
    [string]$SourceRoot = "",
    [string]$OutputRoot = "",
    [string]$BootstrapPythonRoot = "",
    [string]$BootstrapSitePackagesRoot = "",
    [string]$SevenZipRoot = "",
    [switch]$StageOnly,
    [string]$ISCCPath = "",
    [switch]$KeepStaging,
    [switch]$RequireSignedInstaller
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$DefaultSourceRoot = Split-Path -Parent $ScriptDir
$BootstrapPackageEntries = @(
    '*__mypyc*.pyd',
    'certifi',
    'certifi-*.dist-info',
    'charset_normalizer',
    'charset_normalizer-*.dist-info',
    'idna',
    'idna-*.dist-info',
    'packaging',
    'packaging-*.dist-info',
    'psutil',
    'psutil-*.dist-info',
    'pystray',
    'pystray-*.dist-info',
    'requests',
    'requests-*.dist-info',
    'six.py',
    'six-*.dist-info',
    'urllib3',
    'urllib3-*.dist-info'
)

function Write-Step {
    param([Parameter(Mandatory)][string]$Message)
    Write-Host "[Release] $Message" -ForegroundColor Cyan
}

function Get-FullPath {
    param([Parameter(Mandatory)][string]$Path)
    return [System.IO.Path]::GetFullPath($Path)
}

function Assert-SafeChildPath {
    param(
        [Parameter(Mandatory)][string]$Parent,
        [Parameter(Mandatory)][string]$Child,
        [Parameter(Mandatory)][string]$ExpectedLeafPrefix
    )

    $parentFull = (Get-FullPath $Parent).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    $childFull = Get-FullPath $Child
    $prefix = $parentFull + [System.IO.Path]::DirectorySeparatorChar
    if (-not $childFull.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing unsafe path outside output root: $childFull"
    }
    if (-not (Split-Path -Leaf $childFull).StartsWith(
        $ExpectedLeafPrefix,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Refusing unexpected staging path: $childFull"
    }
}

function Remove-SafeTree {
    param(
        [Parameter(Mandatory)][string]$OutputBase,
        [Parameter(Mandatory)][string]$Target
    )

    Assert-SafeChildPath -Parent $OutputBase -Child $Target -ExpectedLeafPrefix "LingJingAI-"
    if (Test-Path -LiteralPath $Target) {
        Remove-Item -LiteralPath $Target -Recurse -Force
    }
}

function Test-ExcludedRelativePath {
    param(
        [Parameter(Mandatory)][string]$RelativePath,
        [Parameter(Mandatory)][ValidateSet("Application", "Workflow", "BootstrapPython")]
        [string]$Profile
    )

    $normal = $RelativePath.Replace('/', '\').TrimStart('\')
    $segments = @($normal.Split('\', [System.StringSplitOptions]::RemoveEmptyEntries))
    $leaf = if ($segments.Count) { $segments[-1] } else { "" }

    if ($segments | Where-Object { $_ -in @('.git', '.gitnexus', '__pycache__') }) {
        return $true
    }
    if ($leaf -match '(?i)\.(pyc|pyo)$') {
        return $true
    }
    if ($leaf -match '(?i)^git\.exe$') {
        return $true
    }
    if (
        $leaf -match '(?i)^(session|account_session|client_instance|workflow_config)\.json$' -or
        $leaf -match '(?i)^config\.local\.(json|txt)$'
    ) {
        return $true
    }
    if ($leaf -match '(?i)^\.(session|workflow_config)\.lock$' -or $leaf -match '(?i)^gateway\.lock$') {
        return $true
    }

    $first = if ($segments.Count) { $segments[0].ToLowerInvariant() } else { "" }
    $mutableRoots = @('models', 'inputs', 'outputs', 'logs', 'tasks', 'temp', 'cache')
    if ($Profile -in @('Application', 'Workflow', 'BootstrapPython') -and $first -in $mutableRoots) {
        return $true
    }
    if ($Profile -eq 'BootstrapPython') {
        if ($first -in @('scripts', 'idlelib', 'turtledemo', 'ensurepip')) {
            return $true
        }
        if ($leaf -match '(?i)^_test.*\.pyd$') {
            return $true
        }
        $portablePackage = $normal.ToLowerInvariant()
        if ($portablePackage -match '^lib\\site-packages\\comfy[^\\]*(?:\\|$)') {
            return $true
        }
        if ($portablePackage -match '^lib\\site-packages\\(?:diffusers|pip|setuptools|_distutils_hack)(?:[-.\\]|$)') {
            return $true
        }
        if ($portablePackage -eq 'lib\site-packages\distutils-precedence.pth') {
            return $true
        }
    }

    return $false
}

function Copy-AllowlistedTree {
    param(
        [Parameter(Mandatory)][string]$Source,
        [Parameter(Mandatory)][string]$Destination,
        [Parameter(Mandatory)][ValidateSet("Application", "Workflow", "BootstrapPython")]
        [string]$Profile
    )

    if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
        throw "Required source directory is missing: $Source"
    }

    $sourceFull = (Get-FullPath $Source).TrimEnd('\')
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    foreach ($item in Get-ChildItem -LiteralPath $sourceFull -Recurse -Force) {
        $relative = $item.FullName.Substring($sourceFull.Length).TrimStart('\')
        if (Test-ExcludedRelativePath -RelativePath $relative -Profile $Profile) {
            continue
        }
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Reparse points are not allowed in release input: $($item.FullName)"
        }

        $target = Join-Path $Destination $relative
        if ($item.PSIsContainer) {
            New-Item -ItemType Directory -Path $target -Force | Out-Null
        }
        else {
            $targetParent = Split-Path -Parent $target
            New-Item -ItemType Directory -Path $targetParent -Force | Out-Null
            Copy-Item -LiteralPath $item.FullName -Destination $target -Force
        }
    }
}

function Copy-BootstrapPackages {
    param(
        [Parameter(Mandatory)][string]$Source,
        [Parameter(Mandatory)][string]$Destination
    )

    if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
        throw "Bootstrap site-packages directory is missing: $Source"
    }
    $sourceItems = @(Get-ChildItem -LiteralPath $Source -Force)
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    $copied = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::OrdinalIgnoreCase
    )
    foreach ($pattern in $BootstrapPackageEntries) {
        $matches = @($sourceItems | Where-Object { $_.Name -like $pattern })
        if (-not $matches.Count) {
            throw "Required bootstrap package entry is missing: $pattern"
        }
        foreach ($item in $matches) {
            if (-not $copied.Add($item.FullName)) {
                continue
            }
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Reparse points are not allowed in bootstrap packages: $($item.FullName)"
            }
            $target = Join-Path $Destination $item.Name
            if ($item.PSIsContainer) {
                Copy-AllowlistedTree -Source $item.FullName -Destination $target -Profile Application
            }
            else {
                Copy-RequiredFile -Source $item.FullName -Destination $target
            }
        }
    }
}

function Assert-StagedClientImportable {
    param([Parameter(Mandatory)][string]$StageRoot)

    $python = Join-Path $StageRoot 'runtime\python\python.exe'
    $entrypoint = Join-Path $StageRoot 'app\gui\main_gateway.py'
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $python
    $startInfo.WorkingDirectory = $StageRoot
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    foreach ($argument in @(
        '-s',
        '-B',
        '-c',
        "import charset_normalizer, pathlib, runpy, sys; root = pathlib.Path.cwd().resolve(); runpy.run_path(sys.argv[1], run_name='__release_startup_smoke__'); modules = (charset_normalizer, sys.modules['requests']); assert all(pathlib.Path(module.__file__).resolve().is_relative_to(root) for module in modules), tuple(module.__file__ for module in modules); print('LINGJING_STARTUP_IMPORT_OK')",
        $entrypoint
    )) {
        $startInfo.ArgumentList.Add($argument)
    }
    $null = $startInfo.Environment.Remove('PYTHONHOME')
    $null = $startInfo.Environment.Remove('PYTHONPATH')
    $startInfo.Environment['PYTHONNOUSERSITE'] = '1'
    $startInfo.Environment['PYTHONDONTWRITEBYTECODE'] = '1'
    $startInfo.Environment['PYTHONUTF8'] = '1'

    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) {
            throw "Staged client startup smoke test could not start Python"
        }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(30000)) {
            $process.Kill($true)
            $process.WaitForExit()
            throw "Staged client startup import timed out after 30 seconds"
        }
        $stdout = $stdoutTask.GetAwaiter().GetResult()
        $stderr = $stderrTask.GetAwaiter().GetResult()
        if ($process.ExitCode -ne 0) {
            throw "Staged client startup import failed with exit code $($process.ExitCode): $stderr"
        }
        if (-not $stdout.Contains('LINGJING_STARTUP_IMPORT_OK')) {
            throw "Staged client startup import did not emit its success marker. Output: $stdout"
        }
    }
    finally {
        $process.Dispose()
    }
}

function Resolve-SevenZipRoot {
    param(
        [string]$RequestedRoot,
        [Parameter(Mandatory)][string]$ProjectRoot
    )

    $candidates = @()
    if ($RequestedRoot) {
        $candidates += $RequestedRoot
    }
    $candidates += @(
        (Join-Path $ProjectRoot 'bin'),
        (Join-Path $env:ProgramFiles '7-Zip'),
        (Join-Path ${env:ProgramFiles(x86)} '7-Zip')
    )
    $command = Get-Command 7z.exe -ErrorAction SilentlyContinue
    if ($command) {
        $candidates += Split-Path -Parent $command.Source
    }
    foreach ($candidate in $candidates) {
        if (-not $candidate) {
            continue
        }
        $full = Get-FullPath $candidate
        if (
            (Test-Path -LiteralPath (Join-Path $full '7z.exe') -PathType Leaf) -and
            (Test-Path -LiteralPath (Join-Path $full '7z.dll') -PathType Leaf) -and
            (Test-Path -LiteralPath (Join-Path $full 'License.txt') -PathType Leaf)
        ) {
            return $full
        }
    }
    throw "7-Zip runtime files were not found. Pass -SevenZipRoot with 7z.exe, 7z.dll and License.txt."
}

function Resolve-PipLicensePath {
    param([Parameter(Mandatory)][string]$SitePackagesRoot)

    if (-not (Test-Path -LiteralPath $SitePackagesRoot -PathType Container)) {
        throw "Bootstrap Python site-packages directory is missing: $SitePackagesRoot"
    }
    $distInfoDirectories = @(
        Get-ChildItem -LiteralPath $SitePackagesRoot -Directory -Filter 'pip-*.dist-info' -Force |
            Sort-Object Name -Descending
    )
    foreach ($distInfo in $distInfoDirectories) {
        foreach ($relative in @('licenses\LICENSE.txt', 'LICENSE.txt')) {
            $candidate = Join-Path $distInfo.FullName $relative
            if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                return $candidate
            }
        }
    }
    throw "pip LICENSE.txt was not found under: $SitePackagesRoot"
}

function Copy-RequiredFile {
    param(
        [Parameter(Mandatory)][string]$Source,
        [Parameter(Mandatory)][string]$Destination
    )

    if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) {
        throw "Required release file is missing: $Source"
    }
    if ((Get-Item -LiteralPath $Source -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
        throw "Reparse points are not allowed in release input: $Source"
    }
    New-Item -ItemType Directory -Path (Split-Path -Parent $Destination) -Force | Out-Null
    Copy-Item -LiteralPath $Source -Destination $Destination -Force
}

function Get-StagedRelativePath {
    param(
        [Parameter(Mandatory)][string]$StageRoot,
        [Parameter(Mandatory)][string]$FilePath
    )
    return $FilePath.Substring($StageRoot.TrimEnd('\').Length).TrimStart('\').Replace('\', '/')
}

function Assert-StagingPolicy {
    param([Parameter(Mandatory)][string]$StageRoot)

    $forbiddenPathPatterns = @(
        '(^|/)(\.git|\.gitnexus|__pycache__)(/|$)',
        '(^|/)\.(env)(\.|$)',
        '\.(pyc|pyo|pfx|p12|key|token)$',
        '^(models|inputs|outputs|logs|tasks|temp|cache)(/|$)',
        '^runtime/(requests|inputs|outputs|logs|tasks|temp|cache|env-backups|ui-review|workflow_import_tmp)(/|$)',
        '^runtime/comfyui(/|$)',
        '(^|/)git\.exe$',
        '^app/updater_runtime/(?!pip(?:/|$)|pip-license\.txt$)',
        '^runtime/python/lib/site-packages/(?:diffusers|pip|setuptools|_distutils_hack)(?:[-./]|$)',
        '^runtime/python/lib/site-packages/comfy[^/]*(?:/|$)',
        '^\.venv/share(/|$)',
        '^\.venv/lib/site-packages/(?:torch|torchaudio|torchvision|triton|nvidia)(?:[-./]|$)',
        '^bin/cloudflared\.exe$',
        '(^|/)(session|account_session|client_instance|workflow_config)\.json$',
        '(^|/)config\.local\.(json|txt)$',
        '(^|/)\.(session|workflow_config)\.lock$',
        '(^|/)gateway\.lock$'
    )

    $secretPatterns = [ordered]@{
        'private-key' = '-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----'
        'github-token' = '\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,})\b'
        'aws-access-key' = '\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'
        'api-secret' = '\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}\b'
    }
    $textExtensions = @(
        '.py', '.json', '.yaml', '.yml', '.md', '.txt', '.bat', '.cmd',
        '.ps1', '.iss', '.toml', '.ini', '.cfg', '.html', '.js', '.css',
        '.svg', '.xml'
    )

    foreach ($file in Get-ChildItem -LiteralPath $StageRoot -Recurse -File -Force) {
        $relative = (Get-StagedRelativePath -StageRoot $StageRoot -FilePath $file.FullName).ToLowerInvariant()
        foreach ($pattern in $forbiddenPathPatterns) {
            if ($relative -match $pattern) {
                throw "Forbidden release member detected: $relative"
            }
        }

        if ($file.Extension.ToLowerInvariant() -in $textExtensions -and $file.Length -le 16MB) {
            $content = Get-Content -LiteralPath $file.FullName -Raw -ErrorAction Stop
            foreach ($entry in $secretPatterns.GetEnumerator()) {
                if ($content -match $entry.Value) {
                    throw "Possible $($entry.Key) detected in release member: $relative"
                }
            }
        }
    }
}

function Resolve-ISCC {
    param([string]$RequestedPath)

    if ($RequestedPath) {
        if (-not (Test-Path -LiteralPath $RequestedPath -PathType Leaf)) {
            throw "ISCC.exe was not found: $RequestedPath"
        }
        return (Get-FullPath $RequestedPath)
    }

    $command = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }
    foreach ($candidate in @(
        (Join-Path $env:ProgramFiles 'Inno Setup 7\ISCC.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 7\ISCC.exe'),
        (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe')
    )) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return $candidate
        }
    }
    throw "Inno Setup 7 or 6 was not found. Install it, pass -ISCCPath, or use -StageOnly."
}

$SourceRoot = if ($SourceRoot) { Get-FullPath $SourceRoot } else { Get-FullPath $DefaultSourceRoot }
if (-not (Test-Path -LiteralPath $SourceRoot -PathType Container)) {
    throw "Source root does not exist: $SourceRoot"
}
$BootstrapPythonRoot = if ($BootstrapPythonRoot) {
    Get-FullPath $BootstrapPythonRoot
}
else {
    Get-FullPath (Join-Path $SourceRoot 'runtime\python')
}
$BootstrapSitePackagesRoot = if ($BootstrapSitePackagesRoot) {
    Get-FullPath $BootstrapSitePackagesRoot
}
else {
    Get-FullPath (Join-Path $SourceRoot '.venv\Lib\site-packages')
}
$SevenZipRoot = Resolve-SevenZipRoot -RequestedRoot $SevenZipRoot -ProjectRoot $SourceRoot
$UpdaterPipSitePackagesRoot = Get-FullPath (Join-Path $BootstrapPythonRoot 'Lib\site-packages')
$UpdaterPipRoot = Get-FullPath (Join-Path $UpdaterPipSitePackagesRoot 'pip')
if (-not (Test-Path -LiteralPath $UpdaterPipRoot -PathType Container)) {
    throw "Bootstrap Python pip package is missing: $UpdaterPipRoot"
}
$UpdaterPipLicensePath = Resolve-PipLicensePath -SitePackagesRoot $UpdaterPipSitePackagesRoot

$versionFile = Join-Path $SourceRoot 'VERSION'
if (-not (Test-Path -LiteralPath $versionFile -PathType Leaf)) {
    throw "VERSION file is missing: $versionFile"
}
$sourceVersion = (Get-Content -LiteralPath $versionFile -Raw).Trim()
if (-not $Version) {
    $Version = $sourceVersion
}
if ($Version -notmatch '^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$') {
    throw "Version is not a safe semantic version: $Version"
}
if ($Version -ne $sourceVersion) {
    throw "Requested version $Version does not match VERSION file $sourceVersion"
}

$OutputRoot = if ($OutputRoot) { Get-FullPath $OutputRoot } else { Join-Path $SourceRoot 'dist' }
New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null
$OutputRoot = Get-FullPath $OutputRoot

$ProductFolder = "LingJingAI-$Version-win-x64"
$StagingBase = Join-Path $OutputRoot 'staging'
$StageRoot = Join-Path $StagingBase $ProductFolder
$ManifestPath = Join-Path $OutputRoot "$ProductFolder.members.json"
$ManifestHashPath = "$ManifestPath.sha256"
New-Item -ItemType Directory -Path $StagingBase -Force | Out-Null
Remove-SafeTree -OutputBase $StagingBase -Target $StageRoot
New-Item -ItemType Directory -Path $StageRoot -Force | Out-Null
foreach ($oldManifest in @($ManifestPath, $ManifestHashPath)) {
    $oldManifestFull = Get-FullPath $oldManifest
    $outputPrefix = $OutputRoot.TrimEnd('\') + '\'
    if (-not $oldManifestFull.StartsWith($outputPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing unsafe manifest output path: $oldManifestFull"
    }
    if (Test-Path -LiteralPath $oldManifestFull -PathType Leaf) {
        Remove-Item -LiteralPath $oldManifestFull -Force
    }
}

$buildSucceeded = $false
try {
    Write-Step "Validating release inputs"
    $requiredInputs = @(
        'app\gui\main_gateway.py',
        'app\gui\assets\app.ico',
        'app\core\runtime_update.py',
        'app\core\runtime_update_helper.ps1',
        'app\core\comfyui_update.py',
        'app\core\comfyui_update_worker.py',
        'app\comfyui_release.json',
        'workflows',
        'start.bat',
        'check-env.bat',
        'README.md',
        'README_EN.md',
        'LICENSE',
        'THIRD_PARTY_NOTICES.md',
        '开源许可说明.txt',
        'docs\灵境造片厂使用教学.pdf',
        'examples\灵境造片厂示例页.html',
        'requirements.lock',
        'requirements-runtime.lock',
        'VERSION'
    )
    $missing = @($requiredInputs | Where-Object { -not (Test-Path -LiteralPath (Join-Path $SourceRoot $_)) })
    if ($missing.Count) {
        throw "Release input is incomplete. Missing: $($missing -join ', ')"
    }
    foreach ($bootstrapExecutable in @('python.exe', 'pythonw.exe', 'python313._pth')) {
        if (-not (Test-Path -LiteralPath (Join-Path $BootstrapPythonRoot $bootstrapExecutable) -PathType Leaf)) {
            throw "Bootstrap Python is incomplete. Missing: $bootstrapExecutable"
        }
    }

    Write-Step "Copying application allowlist"
    Copy-AllowlistedTree -Source (Join-Path $SourceRoot 'app') -Destination (Join-Path $StageRoot 'app') -Profile Application
    Copy-AllowlistedTree `
        -Source $UpdaterPipRoot `
        -Destination (Join-Path $StageRoot 'app\updater_runtime\pip') `
        -Profile Application
    Copy-RequiredFile `
        -Source $UpdaterPipLicensePath `
        -Destination (Join-Path $StageRoot 'app\updater_runtime\pip-LICENSE.txt')
    Copy-AllowlistedTree -Source (Join-Path $SourceRoot 'workflows') -Destination (Join-Path $StageRoot 'workflows') -Profile Workflow
    Copy-AllowlistedTree -Source $BootstrapPythonRoot -Destination (Join-Path $StageRoot 'runtime\python') -Profile BootstrapPython
    Copy-BootstrapPackages -Source $BootstrapSitePackagesRoot -Destination (Join-Path $StageRoot '.venv\Lib\site-packages')

    Copy-RequiredFile -Source (Join-Path $SevenZipRoot '7z.exe') -Destination (Join-Path $StageRoot 'bin\7z.exe')
    Copy-RequiredFile -Source (Join-Path $SevenZipRoot '7z.dll') -Destination (Join-Path $StageRoot 'bin\7z.dll')
    Copy-RequiredFile -Source (Join-Path $SevenZipRoot 'License.txt') -Destination (Join-Path $StageRoot 'bin\7-Zip-License.txt')
    foreach ($rootFile in @(
        'start.bat',
        'check-env.bat',
        'README.md',
        'requirements.txt',
        'requirements.lock',
        'requirements-runtime.lock',
        'VERSION',
        'icon.png'
    )) {
        $source = Join-Path $SourceRoot $rootFile
        if (Test-Path -LiteralPath $source -PathType Leaf) {
            Copy-RequiredFile -Source $source -Destination (Join-Path $StageRoot $rootFile)
        }
    }
    foreach ($requiredNotice in @('LICENSE', 'THIRD_PARTY_NOTICES.md', '开源许可说明.txt')) {
        $source = Join-Path $SourceRoot $requiredNotice
        Copy-RequiredFile -Source $source -Destination (Join-Path $StageRoot $requiredNotice)
    }
    foreach ($optionalNotice in @('LICENSE.txt', 'CHANGELOG.md')) {
        $source = Join-Path $SourceRoot $optionalNotice
        if (Test-Path -LiteralPath $source -PathType Leaf) {
            Copy-RequiredFile -Source $source -Destination (Join-Path $StageRoot $optionalNotice)
        }
    }
    Copy-RequiredFile `
        -Source (Join-Path $SourceRoot 'docs\灵境造片厂使用教学.pdf') `
        -Destination (Join-Path $StageRoot '灵境造片厂使用教学.pdf')
    Copy-RequiredFile `
        -Source (Join-Path $SourceRoot 'examples\灵境造片厂示例页.html') `
        -Destination (Join-Path $StageRoot '灵境造片厂示例页.html')
    foreach ($screenshot in @('console.png', 'workflow-models.png', 'studio.png')) {
        Copy-RequiredFile `
            -Source (Join-Path $SourceRoot "docs\images\2.0\$screenshot") `
            -Destination (Join-Path $StageRoot "docs\images\2.0\$screenshot")
    }

    $releaseInfo = [ordered]@{
        product = 'LingJingAI'
        version = $Version
        platform = 'windows-x64'
        runtime_layout = 'bootstrap-python-separate-ai-runtime'
        environment_included = $false
        models_included = $false
        generated_utc = [DateTime]::UtcNow.ToString('o')
    }
    $releaseInfo | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $StageRoot 'release-info.json') -Encoding utf8NoBOM

    Write-Step "Scanning forbidden paths and possible secrets"
    Assert-StagingPolicy -StageRoot $StageRoot

    $requiredStagedFiles = @(
        'app\gui\main_gateway.py',
        'app\gui\assets\app.ico',
        'app\core\runtime_update.py',
        'app\core\runtime_update_helper.ps1',
        'app\core\comfyui_update.py',
        'app\core\comfyui_update_worker.py',
        'app\comfyui_release.json',
        'app\updater_runtime\pip\__main__.py',
        'app\updater_runtime\pip-LICENSE.txt',
        'runtime\python\python.exe',
        'runtime\python\pythonw.exe',
        '.venv\Lib\site-packages\certifi\__init__.py',
        '.venv\Lib\site-packages\charset_normalizer\__init__.py',
        '.venv\Lib\site-packages\idna\__init__.py',
        '.venv\Lib\site-packages\psutil\__init__.py',
        '.venv\Lib\site-packages\pystray\__init__.py',
        '.venv\Lib\site-packages\requests\__init__.py',
        '.venv\Lib\site-packages\six.py',
        '.venv\Lib\site-packages\urllib3\__init__.py',
        'bin\7z.exe',
        'bin\7z.dll',
        'bin\7-Zip-License.txt',
        'start.bat',
        'check-env.bat',
        'README.md',
        'LICENSE',
        'THIRD_PARTY_NOTICES.md',
        '开源许可说明.txt',
        '灵境造片厂使用教学.pdf',
        '灵境造片厂示例页.html',
        'requirements.lock',
        'requirements-runtime.lock',
        'VERSION',
        'release-info.json'
    )
    $missingStaged = @($requiredStagedFiles | Where-Object {
        -not (Test-Path -LiteralPath (Join-Path $StageRoot $_) -PathType Leaf)
    })
    if ($missingStaged.Count) {
        throw "Staging validation failed. Missing: $($missingStaged -join ', ')"
    }

    Write-Step "Smoke-testing staged client startup imports"
    Assert-StagedClientImportable -StageRoot $StageRoot

    Write-Step "Rechecking staging after startup smoke test"
    Assert-StagingPolicy -StageRoot $StageRoot

    Write-Step "Generating member manifest with SHA256"
    $members = @(
        Get-ChildItem -LiteralPath $StageRoot -Recurse -File -Force |
            Sort-Object FullName |
            ForEach-Object {
                [ordered]@{
                    path = Get-StagedRelativePath -StageRoot $StageRoot -FilePath $_.FullName
                    size_bytes = $_.Length
                    sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
                }
            }
    )
    if (-not $members.Count) {
        throw "Staging is empty"
    }

    $manifest = [ordered]@{
        product = 'LingJingAI'
        version = $Version
        platform = 'windows-x64'
        member_count = $members.Count
        members = $members
    }
    $manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $ManifestPath -Encoding utf8NoBOM
    $manifestHash = (Get-FileHash -LiteralPath $ManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
    "$manifestHash  $(Split-Path -Leaf $ManifestPath)" |
        Set-Content -LiteralPath $ManifestHashPath -Encoding ascii

    if ($StageOnly) {
        $buildSucceeded = $true
        Write-Step "Stage-only build complete: $StageRoot"
        Write-Step "Member manifest: $ManifestPath"
        return
    }

    $compiler = Resolve-ISCC -RequestedPath $ISCCPath
    $installerScript = Join-Path $SourceRoot 'installer\LingJing.iss'
    if (-not (Test-Path -LiteralPath $installerScript -PathType Leaf)) {
        throw "Installer script is missing: $installerScript"
    }

    $installerPath = Join-Path $OutputRoot "LingJingAI-Setup-$Version-win-x64.exe"
    $installerHashPath = "$installerPath.sha256"
    foreach ($oldOutput in @($installerPath, $installerHashPath)) {
        $oldOutputFull = Get-FullPath $oldOutput
        $outputPrefix = $OutputRoot.TrimEnd('\') + '\'
        if (-not $oldOutputFull.StartsWith($outputPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing unsafe installer output path: $oldOutputFull"
        }
        if (Test-Path -LiteralPath $oldOutputFull -PathType Leaf) {
            Remove-Item -LiteralPath $oldOutputFull -Force
        }
    }

    Write-Step "Compiling lightweight bootstrap installer"
    $compilerArgs = @(
        "/DMyAppVersion=$Version",
        "/DStageDir=$StageRoot",
        "/DReleaseOutputDir=$OutputRoot",
        $installerScript
    )
    & $compiler @compilerArgs
    if ($LASTEXITCODE -ne 0) {
        throw "ISCC.exe failed with exit code $LASTEXITCODE"
    }

    if (-not (Test-Path -LiteralPath $installerPath -PathType Leaf)) {
        throw "Expected installer output was not created: $installerPath"
    }
    $installer = Get-Item -LiteralPath $installerPath
    if ($installer.Length -ge 2GB) {
        Remove-Item -LiteralPath $installerPath -Force
        throw "Installer exceeds GitHub's 2 GiB per-asset limit: $($installer.Length) bytes"
    }

    $signature = Get-AuthenticodeSignature -LiteralPath $installerPath
    if ($signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid) {
        if ($RequireSignedInstaller) {
            Remove-Item -LiteralPath $installerPath -Force
            throw "Installer signature is not valid: $($signature.Status)"
        }
        Write-Warning "Installer is not Authenticode-signed. Do not publish it as a trusted release until it is signed."
    }

    $installerHash = (Get-FileHash -LiteralPath $installerPath -Algorithm SHA256).Hash.ToLowerInvariant()
    "$installerHash  $($installer.Name)" |
        Set-Content -LiteralPath $installerHashPath -Encoding ascii

    $buildSucceeded = $true
    Write-Step "Installer: $installerPath"
    Write-Step "SHA256: $installerHash"
}
finally {
    $removeStage = (-not $buildSucceeded) -or (-not $StageOnly -and -not $KeepStaging)
    if ($removeStage -and (Test-Path -LiteralPath $StageRoot)) {
        Remove-SafeTree -OutputBase $StagingBase -Target $StageRoot
    }
    if (-not $buildSucceeded) {
        foreach ($failedManifest in @($ManifestPath, $ManifestHashPath)) {
            if (Test-Path -LiteralPath $failedManifest -PathType Leaf) {
                Remove-Item -LiteralPath $failedManifest -Force
            }
        }
    }
}
