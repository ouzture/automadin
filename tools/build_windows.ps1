[CmdletBinding()]
param(
    [string]$Python,
    [switch]$SkipCompile,
    [switch]$SkipPortableArchive,
    [switch]$SkipInstaller,
    [switch]$RequireInstaller,
    [switch]$RequireComplianceReady,
    [switch]$DownloadCorrespondingSource,
    [string]$InnoCompiler
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$DistRoot = Join-Path $ProjectRoot "dist"
$ExpectedAppDir = Join-Path $DistRoot "Automadin.dist"
$GeneratedExecutablePath = Join-Path $ExpectedAppDir "main.exe"
$ExecutablePath = Join-Path $ExpectedAppDir "Automadin.exe"

function Resolve-PythonExecutable {
    param([string]$ExplicitPython)

    if ($ExplicitPython) {
        $ExplicitCommand = Get-Command $ExplicitPython -ErrorAction SilentlyContinue
        if (-not $ExplicitCommand -or $ExplicitCommand.CommandType -eq "Alias") {
            throw "Python executable not found: $ExplicitPython"
        }
        return $ExplicitCommand.Source
    }

    foreach ($Candidate in @("python.exe", "python")) {
        $PathCommand = Get-Command $Candidate -ErrorAction SilentlyContinue
        if ($PathCommand -and $PathCommand.CommandType -ne "Alias") {
            return $PathCommand.Source
        }
    }
    throw "Python was not found on PATH. Pass -Python with an interpreter path."
}

function Find-InnoCompiler {
    param([string]$ExplicitPath)

    if ($ExplicitPath) {
        if (-not (Test-Path -LiteralPath $ExplicitPath -PathType Leaf)) {
            throw "Inno Setup compiler not found: $ExplicitPath"
        }
        return (Resolve-Path -LiteralPath $ExplicitPath).Path
    }

    $Candidates = @()
    foreach ($VersionDirectory in @("Inno Setup 7", "Inno Setup 6")) {
        if (${env:ProgramFiles(x86)}) {
            $Candidates += Join-Path ${env:ProgramFiles(x86)} "$VersionDirectory\ISCC.exe"
        }
        if ($env:ProgramFiles) {
            $Candidates += Join-Path $env:ProgramFiles "$VersionDirectory\ISCC.exe"
        }
        if ($env:LOCALAPPDATA) {
            $Candidates += Join-Path $env:LOCALAPPDATA "Programs\$VersionDirectory\ISCC.exe"
        }
    }

    foreach ($Candidate in $Candidates) {
        if (Test-Path -LiteralPath $Candidate -PathType Leaf) {
            return (Resolve-Path -LiteralPath $Candidate).Path
        }
    }

    # Chocolatey exposes ISCC.exe through a shim in its bin directory. The
    # compiler can run through that shim, but native release inventory must
    # read the license and version evidence beside the real installation.
    # Prefer the verified installation candidates above and use PATH only as a
    # final fallback for non-shim/custom installations.
    $PathCommand = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
    if ($PathCommand) {
        return $PathCommand.Source
    }
    return $null
}

function Resolve-PySideDeployExecutable {
    param([string]$PythonExecutable)

    $PythonDirectory = Split-Path $PythonExecutable
    $Candidates = @(
        (Join-Path $PythonDirectory "pyside6-deploy.exe"),
        (Join-Path $PythonDirectory "Scripts\pyside6-deploy.exe")
    )
    foreach ($Candidate in $Candidates) {
        if (Test-Path -LiteralPath $Candidate -PathType Leaf) {
            return (Resolve-Path -LiteralPath $Candidate).Path
        }
    }
    throw "pyside6-deploy is not installed for: $PythonExecutable"
}

function Convert-ToFourPartVersion {
    param(
        [string]$VersionText,
        [string]$Label
    )

    if ($VersionText -notmatch '^\s*(\d+(?:\.\d+){0,3})\s*$') {
        throw "$Label is not a numeric Windows version: $VersionText"
    }
    try {
        $Parsed = [Version]$Matches[1]
    } catch {
        throw "$Label could not be parsed as a Windows version: $VersionText"
    }
    $Minor = if ($Parsed.Minor -ge 0) { $Parsed.Minor } else { 0 }
    $Build = if ($Parsed.Build -ge 0) { $Parsed.Build } else { 0 }
    $Revision = if ($Parsed.Revision -ge 0) { $Parsed.Revision } else { 0 }
    return "$($Parsed.Major).$Minor.$Build.$Revision"
}

function Assert-ReleaseExecutableVersion {
    param(
        [string]$Executable,
        [string]$ExpectedVersion
    )

    $Expected = Convert-ToFourPartVersion $ExpectedVersion "Project version"
    $VersionInfo = (Get-Item -LiteralPath $Executable).VersionInfo
    foreach ($PropertyName in @("FileVersion", "ProductVersion")) {
        $Actual = Convert-ToFourPartVersion (
            $VersionInfo.$PropertyName
        ) "Automadin.exe $PropertyName"
        if ($Actual -cne $Expected) {
            throw (
                "Automadin.exe $PropertyName $Actual does not match project " +
                "version $Expected. Recompile instead of reusing stale output."
            )
        }
    }
}

$Python = Resolve-PythonExecutable $Python
$Deploy = Resolve-PySideDeployExecutable $Python
& $Python (Join-Path $ProjectRoot "tools\verify_release_environment.py") `
    (Join-Path $ProjectRoot "requirements-release-windows.txt")
if ($LASTEXITCODE -ne 0) { throw "Release build environment verification failed." }
if ($SkipInstaller -and $RequireInstaller) {
    throw "-SkipInstaller and -RequireInstaller cannot be used together."
}
if ($SkipInstaller -and $InnoCompiler) {
    throw "-SkipInstaller and -InnoCompiler cannot be used together."
}
$ResolvedInnoCompiler = $null
if (-not $SkipInstaller) {
    $ResolvedInnoCompiler = Find-InnoCompiler $InnoCompiler
    if (-not $ResolvedInnoCompiler) {
        $MissingCompilerMessage = (
            "Inno Setup 6.7 or newer compiler (ISCC.exe) was not found. " +
            "Install it or pass -InnoCompiler."
        )
        if ($RequireInstaller) {
            throw "Installer required but $MissingCompilerMessage"
        }
        Write-Warning "Installer skipped: $MissingCompilerMessage"
    }
}

$VersionOutput = & $Python (Join-Path $ProjectRoot "tools\project_metadata.py") --version
if ($LASTEXITCODE -ne 0) { throw "Could not read the project version." }
$AppVersion = ($VersionOutput | Select-Object -Last 1).Trim()
if ($AppVersion -notmatch '^[0-9A-Za-z]+(?:[._+-][0-9A-Za-z]+)*$') {
    throw "Project version is not safe for Windows artifact names: $AppVersion"
}
if ($AppVersion -notmatch '^\d+(?:\.\d+){0,3}$') {
    throw (
        "Project version must contain one to four numeric components for Nuitka " +
        "Windows version metadata: $AppVersion"
    )
}

New-Item -ItemType Directory -Force -Path $DistRoot | Out-Null
$PortableArchive = Join-Path $DistRoot "Automadin-Portable-$AppVersion.zip"
$Installer = Join-Path $DistRoot "installer\Automadin-Setup-$AppVersion.exe"
$SourceSetDir = Join-Path $DistRoot "source\Automadin-$AppVersion"
$SourceManifest = Join-Path $SourceSetDir "corresponding-source-manifest.json"
$SourcePolicy = Join-Path $ProjectRoot "release-source-policy.json"
$ReleaseLock = Join-Path $ProjectRoot "requirements-release-windows.txt"

$PrepareSource = $DownloadCorrespondingSource -or (
    Test-Path -LiteralPath $SourceManifest -PathType Leaf
)
if ($PrepareSource) {
    $SourceArguments = @(
        $SourceSetDir,
        "--project-root", $ProjectRoot,
        "--lock", $ReleaseLock,
        "--policy", $SourcePolicy
    )
    if ($DownloadCorrespondingSource) {
        $SourceArguments += "--download"
    }
    & $Python (Join-Path $ProjectRoot "tools\release_sources.py") @SourceArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Corresponding source preparation failed."
    }
}

# Remove only the exact outputs this invocation promises to recreate. If a later
# gate fails, no same-version archive/installer from an earlier run can be mistaken
# for a current successful release.
if (-not $SkipPortableArchive -and (Test-Path -LiteralPath $PortableArchive)) {
    Remove-Item -LiteralPath $PortableArchive -Force
}
if (-not $SkipInstaller -and (Test-Path -LiteralPath $Installer)) {
    Remove-Item -LiteralPath $Installer -Force
}

if (-not $SkipCompile) {
    # A normal build must start without the previous deployment directory. This makes
    # a successful compiler exit insufficient on its own: the expected executable
    # below can only have been produced by this invocation.
    if (Test-Path -LiteralPath $ExpectedAppDir) {
        Remove-Item -LiteralPath $ExpectedAppDir -Recurse -Force
    }

    $TemporarySpec = Join-Path (
        [System.IO.Path]::GetTempPath()
    ) ("automadin-pysidedeploy-" + [Guid]::NewGuid().ToString("N") + ".spec")
    $SpecTemplate = Get-Content -LiteralPath (
        Join-Path $ProjectRoot "pysidedeploy.spec"
    ) -Raw
    $GeneratedSpec = $SpecTemplate.Replace(
        "__AUTOMADIN_PROJECT_DIR__", $ProjectRoot
    ).Replace(
        "__AUTOMADIN_PYTHON__", $Python
    ).Replace(
        "__AUTOMADIN_VERSION__", $AppVersion
    )
    if ($GeneratedSpec -match '__AUTOMADIN_[A-Z_]+__') {
        throw "Not all deployment spec placeholders were populated."
    }
    Set-Content -LiteralPath $TemporarySpec -Value $GeneratedSpec -Encoding UTF8

    Push-Location $ProjectRoot
    try {
        & $Deploy -c $TemporarySpec -f
        if ($LASTEXITCODE -ne 0) { throw "pyside6-deploy failed." }
    } finally {
        Pop-Location
        if (Test-Path -LiteralPath $TemporarySpec) {
            Remove-Item -LiteralPath $TemporarySpec -Force
        }
    }

    if (Test-Path -LiteralPath $GeneratedExecutablePath -PathType Leaf) {
        if (Test-Path -LiteralPath $ExecutablePath -PathType Leaf) {
            throw "Deployment produced both main.exe and Automadin.exe in the expected output: $ExpectedAppDir"
        }
        Move-Item -LiteralPath $GeneratedExecutablePath -Destination $ExecutablePath
    } elseif (-not (Test-Path -LiteralPath $ExecutablePath -PathType Leaf)) {
        throw (
            "pyside6-deploy completed but did not produce the expected executable. " +
            "Expected: $GeneratedExecutablePath (or $ExecutablePath)"
        )
    }
} else {
    if (-not (Test-Path -LiteralPath $ExecutablePath -PathType Leaf)) {
        throw "-SkipCompile requires the expected existing deployment executable: $ExecutablePath"
    }
}
$AppDir = $ExpectedAppDir
Assert-ReleaseExecutableVersion $ExecutablePath $AppVersion

foreach ($staleRuntime in @((Join-Path $AppDir "browsers"), (Join-Path $AppDir "tools\platform-tools"))) {
    if (Test-Path -LiteralPath $staleRuntime) {
        $resolvedRuntime = (Resolve-Path -LiteralPath $staleRuntime).Path
        if (-not $resolvedRuntime.StartsWith($AppDir, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to remove runtime outside application directory: $resolvedRuntime"
        }
        Remove-Item -LiteralPath $resolvedRuntime -Recurse -Force
    }
}

# Keep the companion unpacked so Chrome and Edge can load the exact extension
# shipped with this experimental build. The native-host manifest intentionally
# sits beside Automadin.exe so its Windows-relative executable path is stable.
$CompanionSource = Join-Path $ProjectRoot "automadin\resources\browser_companion_extension"
$CompanionDestination = Join-Path $AppDir "browser-companion-extension"
if (Test-Path -LiteralPath $CompanionDestination) {
    Remove-Item -LiteralPath $CompanionDestination -Recurse -Force
}
Copy-Item -LiteralPath $CompanionSource -Destination $CompanionDestination -Recurse
Copy-Item -LiteralPath (
    Join-Path $ProjectRoot "automadin\resources\browser_companion_native_host.json"
) -Destination (Join-Path $AppDir "browser-companion-native-host.json") -Force

$NativePrepared = $false
$NativePolicy = Join-Path $ProjectRoot "release-native-policy.json"
if ($ResolvedInnoCompiler) {
    $InnoRoot = Split-Path -Parent $ResolvedInnoCompiler
    $InnoLicense = Join-Path $InnoRoot "license.txt"
    $InnoUninstallerTemplate = Join-Path $InnoRoot "unins000.exe"
    if (-not (Test-Path -LiteralPath $InnoLicense -PathType Leaf)) {
        throw "Inno Setup license evidence is missing: $InnoLicense"
    }
    if (-not (Test-Path -LiteralPath $InnoUninstallerTemplate -PathType Leaf)) {
        throw "Inno Setup version evidence is missing: $InnoUninstallerTemplate"
    }
    $InnoVersion = ((Get-Item -LiteralPath $InnoUninstallerTemplate).VersionInfo.ProductVersion -replace '\s', '').Trim()
    if ($InnoVersion -notmatch '^\d+(?:\.\d+){1,3}$') {
        throw "Inno Setup version evidence is invalid: $InnoVersion"
    }
    $PythonVersion = (& $Python -c "import platform; print(platform.python_version())" | Select-Object -Last 1).Trim()
    if ($LASTEXITCODE -ne 0) { throw "Could not read the release Python version." }
    & $Python (Join-Path $ProjectRoot "tools\native_release_inventory.py") `
        $AppDir `
        --project-root $ProjectRoot `
        --lock $ReleaseLock `
        --policy $NativePolicy `
        --python-version $PythonVersion `
        --inno-compiler $ResolvedInnoCompiler `
        --inno-version $InnoVersion `
        --inno-license $InnoLicense
    if ($LASTEXITCODE -ne 0) { throw "Native release inventory failed." }
    $NativePrepared = $true
}

$ComplianceArguments = @(
    $AppDir,
    "--project-root", $ProjectRoot,
    "--lock", $ReleaseLock,
    "--policy", (Join-Path $ProjectRoot "release-license-policy.json")
)
if ($PrepareSource) {
    $ComplianceArguments += @(
        "--source-dir", $SourceSetDir,
        "--source-policy", $SourcePolicy
    )
}
if ($NativePrepared) {
    $ComplianceArguments += @(
        "--native-policy", $NativePolicy,
        "--inno-compiler", $ResolvedInnoCompiler
    )
}
& $Python (Join-Path $ProjectRoot "tools\generate_release_compliance.py") `
    @ComplianceArguments
if ($LASTEXITCODE -ne 0) { throw "Release compliance generation failed." }

& $Python (Join-Path $ProjectRoot "tools\write_release_manifest.py") `
    $AppDir --project-root $ProjectRoot
if ($LASTEXITCODE -ne 0) { throw "Release manifest creation failed." }

& $Python (Join-Path $ProjectRoot "tools\installer_manifest.py") $AppDir
if ($LASTEXITCODE -ne 0) { throw "Installer managed-file manifest creation failed." }

$VerifyReleaseArguments = @(
    $AppDir,
    "--project-root", $ProjectRoot,
    "--lock", (Join-Path $ProjectRoot "requirements-release-windows.txt"),
    "--policy", (Join-Path $ProjectRoot "release-license-policy.json")
)
if ($RequireComplianceReady) {
    $VerifyReleaseArguments += "--require-compliance-ready"
}
if ($PrepareSource) {
    $VerifyReleaseArguments += @(
        "--source-dir", $SourceSetDir,
        "--source-policy", $SourcePolicy
    )
}
if ($NativePrepared) {
    $VerifyReleaseArguments += @(
        "--native-policy", $NativePolicy,
        "--inno-compiler", $ResolvedInnoCompiler
    )
}
& $Python (Join-Path $ProjectRoot "tools\verify_release.py") @VerifyReleaseArguments
if ($LASTEXITCODE -ne 0) { throw "Release verification failed." }

$ScratchParent = Join-Path (
    [System.IO.Path]::GetTempPath()
) ("automadin-release-self-test-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $ScratchParent | Out-Null
try {
    $FallbackDataDir = Join-Path $ScratchParent "fallback-data"
    & $Python -m tools.run_bounded_release_command --timeout 60 `
        $ExecutablePath -- `
        --release-pushover-credential-self-test `
        --data-dir $FallbackDataDir
    $PushoverSelfTestExitCode = $LASTEXITCODE
    if ($PushoverSelfTestExitCode -ne 0) {
        throw "Release Pushover credential self-test failed with exit code $PushoverSelfTestExitCode."
    }
    & $Python -m tools.run_bounded_release_command --timeout 60 `
        $ExecutablePath -- `
        --release-browser-health-self-test $ScratchParent `
        --data-dir $FallbackDataDir
    $SelfTestExitCode = $LASTEXITCODE
    if ($SelfTestExitCode -ne 0) {
        throw "Release browser-health self-test failed with exit code $SelfTestExitCode."
    }
    $DiagnosticsNonce = [Guid]::NewGuid().ToString("N").ToLowerInvariant()
    $DiagnosticsMarker = Join-Path (
        $ScratchParent
    ) "diagnostics-$DiagnosticsNonce.ok"
    & $Python -m tools.run_bounded_release_command --timeout 60 `
        $ExecutablePath -- `
        --release-diagnostics-self-test $ScratchParent `
        --release-self-test-nonce $DiagnosticsNonce `
        --data-dir $FallbackDataDir
    $DiagnosticsSelfTestExitCode = $LASTEXITCODE
    if ($DiagnosticsSelfTestExitCode -ne 0) {
        throw "Release diagnostics self-test failed with exit code $DiagnosticsSelfTestExitCode."
    }
    if (-not (Test-Path -LiteralPath $DiagnosticsMarker -PathType Leaf)) {
        throw "Release diagnostics self-test did not create its nonce-bound marker."
    }
    $ExpectedDiagnosticsMarker = (
        "automadin-diagnostics-self-test-v1:$DiagnosticsNonce`n"
    )
    $ActualDiagnosticsMarker = Get-Content -LiteralPath (
        $DiagnosticsMarker
    ) -Raw
    if ($ActualDiagnosticsMarker -cne $ExpectedDiagnosticsMarker) {
        throw "Release diagnostics self-test marker was invalid."
    }
} finally {
    if (Test-Path -LiteralPath $ScratchParent) {
        Remove-Item -LiteralPath $ScratchParent -Recurse -Force
    }
}

if (-not $SkipPortableArchive) {
    & $Python (Join-Path $ProjectRoot "tools\make_portable.py") $AppDir $PortableArchive
    if ($LASTEXITCODE -ne 0) { throw "Portable archive creation failed." }
}

if ($SkipInstaller) {
    Write-Host "Installer skipped: -SkipInstaller was specified."
} else {
    if ($ResolvedInnoCompiler) {
        $InstallerScript = Join-Path $ProjectRoot "packaging\Automadin.iss"
        $VersionDefine = "/DMyAppVersion=$AppVersion"
        $SourceDefine = "/DSourceDir=$AppDir"
        & $ResolvedInnoCompiler $VersionDefine $SourceDefine $InstallerScript
        if ($LASTEXITCODE -ne 0) { throw "Inno Setup compiler failed." }

        if (-not (Test-Path -LiteralPath $Installer -PathType Leaf)) {
            throw "Inno Setup completed but the expected installer was not found: $Installer"
        }
        Write-Host "Installer ready: $Installer"
    }
}

Write-Host "Standalone release ready: $AppDir"
