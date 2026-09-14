[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$projectRoot = [System.IO.Path]::GetFullPath(
    (Split-Path -Parent $PSScriptRoot)
)
$packageName = "QQ" + [char]0x70AB + [char]0x821E + "3.0"
$customDirectoryName = -join @([char]0x81EA, [char]0x5B9A, [char]0x4E49)
$huntingGroundDirectoryName = (
    -join @([char]0x6253, [char]0x730E, [char]0x573A)
) + "2"
$specPath = Join-Path $projectRoot "$packageName.spec"
$pyinstallerPath = Join-Path $projectRoot ".venv\Scripts\pyinstaller.exe"
$venvPythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$identityToolPath = Join-Path $PSScriptRoot "build_randomize.py"
$identityDir = Join-Path $projectRoot ".build_release_identity"
$distRoot = Join-Path $projectRoot "dist"
$stageRoot = Join-Path $projectRoot ".build_release_stage"
$stageDistRoot = Join-Path $stageRoot "dist"
$preserveRoot = Join-Path $stageRoot "preserve"
$templateBackupDir = Join-Path $preserveRoot "custom_templates"
$routeBackupDir = Join-Path $preserveRoot "recordings"
$settingsBackupPath = Join-Path $preserveRoot "v3_settings.json"
$readmeBackupPath = Join-Path $preserveRoot "README_3.0.md"
$monsterLibrarySource = Join-Path $projectRoot "img\monsters"
$routeRecordingsSource = Join-Path $projectRoot "v3\map\recordings"

# Every build gets a fresh randomized identity (name / icon / copyright).
# These are populated at runtime, after the identity has been generated.
$outputPackageName = $null
$finalPackageDir = $null
$stagePackageDir = $null
$currentPackageDir = $null

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Get-SafeProjectPath {
    param([string]$Path)
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    $rootPrefix = $projectRoot.TrimEnd("\") + "\"
    if (-not $fullPath.StartsWith(
        $rootPrefix,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Refusing to operate outside the project directory: $fullPath"
    }
    return $fullPath
}

function Remove-SafeDirectory {
    param([string]$Path)
    $safePath = Get-SafeProjectPath $Path
    if (Test-Path -LiteralPath $safePath) {
        Remove-Item -LiteralPath $safePath -Recurse -Force
    }
}

function Get-TemplateCount {
    param([string]$Directory)
    if (-not (Test-Path -LiteralPath $Directory -PathType Container)) {
        return 0
    }
    return @(
        Get-ChildItem -LiteralPath $Directory -File -Filter "guai*.png"
    ).Count
}

function Get-MonsterImageCount {
    param([string]$Directory)
    if (-not (Test-Path -LiteralPath $Directory -PathType Container)) {
        return 0
    }
    return @(
        Get-ChildItem -LiteralPath $Directory -Recurse -File |
            Where-Object {
                $_.Extension.ToLowerInvariant() -in @(
                    ".png", ".jpg", ".jpeg", ".bmp", ".webp"
                )
            }
    ).Count
}

function Get-RouteJsonCount {
    param([string]$Directory)
    if (-not (Test-Path -LiteralPath $Directory -PathType Container)) {
        return 0
    }
    return @(
        Get-ChildItem -LiteralPath $Directory -Recurse -File -Filter "*.json"
    ).Count
}

function Copy-DirectoryTree {
    param(
        [string]$SourceDirectory,
        [string]$TargetDirectory
    )
    if (-not (Test-Path -LiteralPath $SourceDirectory -PathType Container)) {
        throw "Resource directory not found: $SourceDirectory"
    }
    if (Test-Path -LiteralPath $TargetDirectory) {
        Remove-SafeDirectory $TargetDirectory
    }
    New-Item -ItemType Directory -Path $TargetDirectory -Force | Out-Null
    Get-ChildItem -LiteralPath $SourceDirectory -Force | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $TargetDirectory -Recurse -Force
    }
}

function Merge-DirectoryTree {
    param(
        [string]$SourceDirectory,
        [string]$TargetDirectory
    )
    if (-not (Test-Path -LiteralPath $SourceDirectory -PathType Container)) {
        return
    }
    New-Item -ItemType Directory -Path $TargetDirectory -Force | Out-Null
    Get-ChildItem -LiteralPath $SourceDirectory -Force | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $TargetDirectory -Recurse -Force
    }
}

function Find-TemplateSource {
    $candidates = @()
    if ($currentPackageDir) {
        $candidates += (Join-Path $currentPackageDir (
            "img\" + $customDirectoryName
        ))
    }
    $candidates += @(
        (Join-Path $projectRoot (
            "img\" + $customDirectoryName + "\" + $huntingGroundDirectoryName
        )),
        (Join-Path $projectRoot ("img\" + $customDirectoryName))
    )
    foreach ($candidate in $candidates) {
        if ((Get-TemplateCount $candidate) -gt 0) {
            return $candidate
        }
    }
    return $null
}

function Copy-InternalTemplates {
    param(
        [string]$SourceDirectory,
        [string]$TargetDirectory
    )
    New-Item -ItemType Directory -Path $TargetDirectory -Force | Out-Null
    Get-ChildItem -LiteralPath $TargetDirectory -File -Filter "guai*.png" |
        Remove-Item -Force
    Get-ChildItem -LiteralPath $SourceDirectory -File -Filter "guai*.png" |
        Copy-Item -Destination $TargetDirectory -Force
}

function Restore-PreviousPackages {
    # Each build uses a random name, so any number of old packages may sit
    # in dist as "<name>.previous" after a failed replacement. Move them all
    # back to their original names.
    if (-not (Test-Path -LiteralPath $distRoot -PathType Container)) {
        return
    }
    Get-ChildItem -LiteralPath $distRoot -Directory |
        Where-Object { $_.Name.EndsWith(".previous") } |
        ForEach-Object {
            $originalName = $_.Name.Substring(
                0, $_.Name.Length - ".previous".Length
            )
            $originalPath = Join-Path $distRoot $originalName
            if (-not (Test-Path -LiteralPath $originalPath)) {
                Move-Item -LiteralPath $_.FullName -Destination $originalPath
            }
        }
}

try {
    Write-Step "Checking the build environment"
    if (-not (Test-Path -LiteralPath $specPath -PathType Leaf)) {
        throw "Build spec not found: $specPath"
    }
    if (-not (Test-Path -LiteralPath $pyinstallerPath -PathType Leaf)) {
        throw "PyInstaller not found: $pyinstallerPath"
    }
    if (-not (Test-Path -LiteralPath $venvPythonPath -PathType Leaf)) {
        throw "Virtual environment Python not found: $venvPythonPath"
    }
    if (-not (Test-Path -LiteralPath $identityToolPath -PathType Leaf)) {
        throw "Random identity tool not found: $identityToolPath"
    }
    $sourceMonsterCount = Get-MonsterImageCount $monsterLibrarySource
    $sourceRouteCount = Get-RouteJsonCount $routeRecordingsSource
    if ($sourceMonsterCount -le 0) {
        throw "Monster atlas has no images: $monsterLibrarySource"
    }
    if ($sourceRouteCount -le 0) {
        throw "Route recordings have no JSON files: $routeRecordingsSource"
    }
    Write-Host "Monster atlas source images: $sourceMonsterCount"
    Write-Host "Route JSON source files: $sourceRouteCount"

    Write-Step "Generating the randomized build identity (name, icon, copyright)"
    if (Test-Path -LiteralPath $identityDir) {
        Remove-SafeDirectory $identityDir
    }
    $identityJson = (& $venvPythonPath $identityToolPath `
        --output-dir $identityDir) | Out-String
    if ($LASTEXITCODE -ne 0) {
        throw "Random identity generation failed with exit code: $LASTEXITCODE"
    }
    $identity = $identityJson | ConvertFrom-Json
    $outputPackageName = [string]$identity.name
    $iconFilePath = [string]$identity.icon
    $versionFilePath = [string]$identity.version_file
    if ([string]::IsNullOrWhiteSpace($outputPackageName)) {
        throw "Random identity tool returned an empty package name."
    }
    Write-Host "Package name for this build: $outputPackageName"
    Write-Host "Icon resource: $iconFilePath"
    Write-Host "Version resource: $versionFilePath"

    $stagePackageDir = Join-Path $stageDistRoot $outputPackageName
    $finalPackageDir = Join-Path $distRoot $outputPackageName

    # Builds rename the package every time, so the previous package may sit
    # under a different (older random) name. Detect all live packages in
    # dist; the newest one is the "current package" whose settings, routes,
    # and templates must be carried over.
    $existingPackageDirs = @()
    $currentPackageDir = $null
    if (Test-Path -LiteralPath $distRoot -PathType Container) {
        $existingPackageDirs = @(
            Get-ChildItem -LiteralPath $distRoot -Directory |
                Where-Object { -not $_.Name.EndsWith(".previous") } |
                Sort-Object -Property LastWriteTime -Descending
        )
    }
    if ($existingPackageDirs.Count -gt 0) {
        $currentPackageDir = $existingPackageDirs[0].FullName
        Write-Host "Current package to carry over: $currentPackageDir"
    }

    Remove-SafeDirectory $stageRoot
    New-Item -ItemType Directory -Path $preserveRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $stageDistRoot -Force | Out-Null

    Write-Step "Preserving settings, documentation, and custom templates"
    $settingsCandidates = @(Join-Path $projectRoot "v3_settings.json")
    if ($currentPackageDir) {
        $settingsCandidates += (Join-Path $currentPackageDir "v3_settings.json")
    }
    foreach ($currentSettings in $settingsCandidates) {
        if (Test-Path -LiteralPath $currentSettings -PathType Leaf) {
            Copy-Item -LiteralPath $currentSettings -Destination $settingsBackupPath -Force
            Write-Host "Preserved current settings from: $currentSettings"
            break
        }
    }

    $readmeCandidates = @()
    if ($currentPackageDir) {
        $readmeCandidates += (Join-Path $currentPackageDir "README_3.0.md")
    }
    $readmeCandidates += (Join-Path $projectRoot "README_3.0.md")
    foreach ($candidate in $readmeCandidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            Copy-Item -LiteralPath $candidate -Destination $readmeBackupPath -Force
            break
        }
    }

    $templateSource = Find-TemplateSource
    if ($null -ne $templateSource) {
        Copy-Item -LiteralPath $templateSource -Destination $templateBackupDir -Recurse
        Write-Host (
            "Preserved {0} guai*.png files from: {1}" -f
            (Get-TemplateCount $templateBackupDir),
            $templateSource
        )
    }
    else {
        Write-Host "No guai*.png files found; spec defaults will be used." -ForegroundColor Yellow
    }

    $currentRouteDirectory = $null
    if ($currentPackageDir) {
        $currentRouteDirectory = Join-Path $currentPackageDir "v3\map\recordings"
    }
    if ($null -ne $currentRouteDirectory -and
        (Test-Path -LiteralPath $currentRouteDirectory -PathType Container)) {
        Copy-DirectoryTree $currentRouteDirectory $routeBackupDir
        Write-Host (
            "Preserved {0} external route JSON files from the current package." -f
            (Get-RouteJsonCount $routeBackupDir)
        )
    }

    Write-Step "Building in staging; the current package remains untouched until success"
    $env:YOLO_CONFIG_DIR = Join-Path $projectRoot ".v3_runtime\ultralytics"
    $env:MPLCONFIGDIR = Join-Path $projectRoot ".v3_runtime\matplotlib"
    New-Item -ItemType Directory -Path $env:YOLO_CONFIG_DIR -Force | Out-Null
    New-Item -ItemType Directory -Path $env:MPLCONFIGDIR -Force | Out-Null

    $env:V3_PACKAGE_NAME = $outputPackageName
    $env:V3_ICON_PATH = $iconFilePath
    $env:V3_VERSION_FILE = $versionFilePath
    & $pyinstallerPath --noconfirm --distpath $stageDistRoot $specPath
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code: $LASTEXITCODE"
    }

    $stageExePath = Join-Path $stagePackageDir "$outputPackageName.exe"
    if (-not (Test-Path -LiteralPath $stageExePath -PathType Leaf)) {
        throw "Build finished without producing the expected EXE: $stageExePath"
    }

    $stageInternalMonsterLibrary = Join-Path $stagePackageDir "_internal\img\monsters"
    $stageInternalRecordings = Join-Path $stagePackageDir "_internal\v3\map\recordings"
    $internalMonsterCount = Get-MonsterImageCount $stageInternalMonsterLibrary
    $internalRouteCount = Get-RouteJsonCount $stageInternalRecordings
    if ($internalMonsterCount -lt $sourceMonsterCount) {
        throw (
            "Internal monster atlas is incomplete: expected at least {0}, found {1}" -f
            $sourceMonsterCount,
            $internalMonsterCount
        )
    }
    if ($internalRouteCount -lt $sourceRouteCount) {
        throw (
            "Internal route JSON set is incomplete: expected at least {0}, found {1}" -f
            $sourceRouteCount,
            $internalRouteCount
        )
    }

    Write-Step "Restoring settings, documentation, and templates"
    if (Test-Path -LiteralPath $settingsBackupPath -PathType Leaf) {
        Copy-Item -LiteralPath $settingsBackupPath -Destination (
            Join-Path $stagePackageDir "v3_settings.json"
        ) -Force
    }
    if (Test-Path -LiteralPath $readmeBackupPath -PathType Leaf) {
        Copy-Item -LiteralPath $readmeBackupPath -Destination (
            Join-Path $stagePackageDir "README_3.0.md"
        ) -Force
    }
    if (Test-Path -LiteralPath $templateBackupDir -PathType Container) {
        $stageExternalParent = Join-Path $stagePackageDir "img"
        $stageExternalTemplates = Join-Path $stageExternalParent $customDirectoryName
        New-Item -ItemType Directory -Path $stageExternalParent -Force | Out-Null
        if (Test-Path -LiteralPath $stageExternalTemplates) {
            Remove-SafeDirectory $stageExternalTemplates
        }
        Copy-Item -LiteralPath $templateBackupDir -Destination $stageExternalTemplates -Recurse

        $stageInternalTemplates = Join-Path $stagePackageDir (
            "_internal\img\" + $customDirectoryName
        )
        Copy-InternalTemplates $templateBackupDir $stageInternalTemplates
    }


    Write-Step "Copying editable route JSON and monster atlas beside the EXE"
    $stageExternalMonsterLibrary = Join-Path $stagePackageDir "img\monsters"
    $stageExternalRecordings = Join-Path $stagePackageDir "v3\map\recordings"
    Copy-DirectoryTree $monsterLibrarySource $stageExternalMonsterLibrary
    Copy-DirectoryTree $routeRecordingsSource $stageExternalRecordings
    Merge-DirectoryTree $routeBackupDir $stageExternalRecordings

    $externalMonsterCount = Get-MonsterImageCount $stageExternalMonsterLibrary
    $externalRouteCount = Get-RouteJsonCount $stageExternalRecordings
    if ($externalMonsterCount -lt $sourceMonsterCount) {
        throw (
            "External monster atlas is incomplete: expected at least {0}, found {1}" -f
            $sourceMonsterCount,
            $externalMonsterCount
        )
    }
    if ($externalRouteCount -lt $sourceRouteCount) {
        throw (
            "External route JSON set is incomplete: expected at least {0}, found {1}" -f
            $sourceRouteCount,
            $externalRouteCount
        )
    }

    Write-Step "Running packaged resource self-test"
    $selfTestProcess = Start-Process `
        -FilePath $stageExePath `
        -ArgumentList "--package-self-test" `
        -PassThru `
        -WindowStyle Hidden
    # The self-test process may hang; fail the build instead of waiting forever.
    $selfTestTimeoutSeconds = 120
    if (-not $selfTestProcess.WaitForExit($selfTestTimeoutSeconds * 1000)) {
        $selfTestProcess.Kill()
        $selfTestProcess.WaitForExit()
        throw (
            "Packaged resource self-test timed out after {0}s" -f
            $selfTestTimeoutSeconds
        )
    }
    if ($selfTestProcess.ExitCode -ne 0) {
        throw (
            "Packaged resource self-test failed with exit code: {0}" -f
            $selfTestProcess.ExitCode
        )
    }

    Write-Step "Replacing the dist package with the verified staged build"
    # Move-Item does not create the parent directory; the first build has no dist.
    if (-not (Test-Path -LiteralPath $distRoot -PathType Container)) {
        New-Item -ItemType Directory -Path $distRoot -Force | Out-Null
    }
    $oldPackageBackups = @()
    try {
        # Park every live package (any name) as "<name>.previous", then move
        # the fresh one in. Old backups are dropped only after success.
        Get-ChildItem -LiteralPath $distRoot -Directory |
            Where-Object { -not $_.Name.EndsWith(".previous") } |
            ForEach-Object {
                $backupPath = Join-Path $distRoot ($_.Name + ".previous")
                Remove-SafeDirectory $backupPath
                Move-Item -LiteralPath $_.FullName -Destination $backupPath
                $oldPackageBackups += $backupPath
            }
        Move-Item -LiteralPath $stagePackageDir -Destination $finalPackageDir

        $finalExePath = Join-Path $finalPackageDir "$outputPackageName.exe"
        if (-not (Test-Path -LiteralPath $finalExePath -PathType Leaf)) {
            throw "The staged package has no EXE; restoring the previous packages."
        }
        foreach ($backupPath in $oldPackageBackups) {
            Remove-SafeDirectory $backupPath
        }
    }
    catch {
        if (Test-Path -LiteralPath $finalPackageDir -PathType Container) {
            # A partially moved package is worthless; drop it so the old
            # packages can be restored to their original names.
            Remove-SafeDirectory $finalPackageDir
        }
        Restore-PreviousPackages
        throw
    }

    Write-Step "Build completed"
    $finalExePath = Join-Path $finalPackageDir "$outputPackageName.exe"
    $exe = Get-Item -LiteralPath $finalExePath
    $hash = Get-FileHash -LiteralPath $finalExePath -Algorithm SHA256
    $externalCount = Get-TemplateCount (
        Join-Path $finalPackageDir ("img\" + $customDirectoryName)
    )
    $internalCount = Get-TemplateCount (
        Join-Path $finalPackageDir ("_internal\img\" + $customDirectoryName)
    )
    $mapTemplateCount = Get-TemplateCount (
        Join-Path $finalPackageDir (
            "_internal\img\" + $customDirectoryName + "\" +
            $huntingGroundDirectoryName
        )
    )
    $finalExternalMonsterCount = Get-MonsterImageCount (
        Join-Path $finalPackageDir "img\monsters"
    )
    $finalInternalMonsterCount = Get-MonsterImageCount (
        Join-Path $finalPackageDir "_internal\img\monsters"
    )
    $finalExternalRouteCount = Get-RouteJsonCount (
        Join-Path $finalPackageDir "v3\map\recordings"
    )
    $finalInternalRouteCount = Get-RouteJsonCount (
        Join-Path $finalPackageDir "_internal\v3\map\recordings"
    )

    Write-Host "EXE: $finalExePath" -ForegroundColor Green
    Write-Host ("Size: {0:N0} bytes" -f $exe.Length)
    Write-Host "SHA-256: $($hash.Hash)"
    Write-Host "External templates: $externalCount"
    Write-Host "Internal templates: $internalCount"
    Write-Host "Map-specific templates: $mapTemplateCount"
    Write-Host "External monster atlas images: $finalExternalMonsterCount"
    Write-Host "Internal monster atlas images: $finalInternalMonsterCount"
    Write-Host "External route JSON files: $finalExternalRouteCount"
    Write-Host "Internal route JSON files: $finalInternalRouteCount"

    Remove-SafeDirectory $stageRoot
    Remove-SafeDirectory $identityDir
    exit 0
}
catch {
    Write-Host ""
    Write-Host "Build failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "The old dist is kept when possible. Staging files: $stageRoot" -ForegroundColor Yellow
    exit 1
}
