param(
    [string]$ProjectPath = "C:\Users\andre\Projects\modguard"
)

$ErrorActionPreference = "Stop"
$PatchRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

if (-not (Test-Path -LiteralPath $ProjectPath)) {
    throw "Project folder not found: $ProjectPath"
}

$ProjectPath = (Resolve-Path -LiteralPath $ProjectPath).Path
$ProjectParent = Split-Path -Parent $ProjectPath
$ExternalBackupBase = Join-Path $ProjectParent "_modguard_patch_backups"
$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$BackupRoot = Join-Path $ExternalBackupBase ("stabilization_v3_cleanup_" + $Timestamp)

New-Item -ItemType Directory -Force -Path $ExternalBackupBase | Out-Null
New-Item -ItemType Directory -Force -Path $BackupRoot | Out-Null

Write-Host "[ModGuard v3 cleanup] Project: $ProjectPath" -ForegroundColor Cyan
Write-Host "[ModGuard v3 cleanup] Backup:  $BackupRoot" -ForegroundColor Cyan

# 1. Move accidental in-project backups outside the pytest project tree.
$BackupPatterns = @(
    "_backup_stabilization_v3_*",
    "_backup_enforcement_v2_*",
    "_backup_modguard_*"
)

foreach ($Pattern in $BackupPatterns) {
    Get-ChildItem -LiteralPath $ProjectPath -Directory -Filter $Pattern -ErrorAction SilentlyContinue | ForEach-Object {
        $Destination = Join-Path $ExternalBackupBase $_.Name
        if (Test-Path -LiteralPath $Destination) {
            $Destination = Join-Path $ExternalBackupBase ($_.Name + "_moved_" + (Get-Date -Format "yyyyMMdd_HHmmssfff"))
        }
        Write-Host "MOVED old in-project backup: $($_.Name)" -ForegroundColor Yellow
        Move-Item -LiteralPath $_.FullName -Destination $Destination -Force
    }
}

# 2. Replace root pytest config and root installer reference.
$Files = @(
    "pytest.ini",
    "APPLY_PATCH.ps1",
    "tests\test_project_hygiene_v3.py"
)

foreach ($Relative in $Files) {
    $Source = Join-Path $PatchRoot $Relative
    $Target = Join-Path $ProjectPath $Relative

    if (-not (Test-Path -LiteralPath $Source)) {
        throw "Patch file missing: $Source"
    }

    if (Test-Path -LiteralPath $Target) {
        $Backup = Join-Path $BackupRoot $Relative
        $BackupParent = Split-Path -Parent $Backup
        if ($BackupParent) {
            New-Item -ItemType Directory -Force -Path $BackupParent | Out-Null
        }
        Copy-Item -LiteralPath $Target -Destination $Backup -Force
    }

    $TargetParent = Split-Path -Parent $Target
    if ($TargetParent) {
        New-Item -ItemType Directory -Force -Path $TargetParent | Out-Null
    }

    Copy-Item -LiteralPath $Source -Destination $Target -Force

    $SourceHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Source).Hash
    $TargetHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Target).Hash
    if ($SourceHash -ne $TargetHash) {
        throw "SHA256 mismatch after copy: $Relative"
    }

    Write-Host "REPLACED $Relative" -ForegroundColor Green
}

# 3. Remove only accidental Explorer duplicate Python tests.
$TestsPath = Join-Path $ProjectPath "tests"
if (Test-Path -LiteralPath $TestsPath) {
    Get-ChildItem -LiteralPath $TestsPath -File -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -match " \(\d+\)\.py$"
    } | ForEach-Object {
        Write-Host "REMOVED duplicate test: $($_.Name)" -ForegroundColor Yellow
        Remove-Item -LiteralPath $_.FullName -Force
    }
}

# 4. Hard preflight: old v3 backup must not still exist inside project root.
$Remaining = Get-ChildItem -LiteralPath $ProjectPath -Directory -Filter "_backup_stabilization_v3_*" -ErrorAction SilentlyContinue
if ($Remaining) {
    throw "Old stabilization backup still exists inside project root. Refusing to run pytest."
}

Write-Host "" 
Write-Host "Project hygiene fixed. Running pytest from tests/ only..." -ForegroundColor Cyan

$Python = Join-Path $ProjectPath ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    $Python = "python"
}

Push-Location $ProjectPath
try {
    & $Python -m pytest -v
    $Code = $LASTEXITCODE
}
finally {
    Pop-Location
}

if ($Code -ne 0) {
    throw "pytest failed with exit code $Code"
}

Write-Host "" 
Write-Host "[ModGuard v3 cleanup] FULL PYTEST GREEN." -ForegroundColor Green
Write-Host "No app/ production files were changed by this cleanup hotfix." -ForegroundColor DarkGreen
