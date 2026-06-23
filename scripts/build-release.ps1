param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9]+\.[0-9]+\.[0-9]+$')]
    [string]$Version
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$appName = 'MISA Auto Tool'
$distDirectory = Join-Path $projectRoot 'dist'
$appDirectory = Join-Path $distDirectory $appName
$releaseDirectory = Join-Path $projectRoot 'release'
$archivePath = Join-Path $releaseDirectory "MISA-Auto-Tool-v$Version.zip"
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    $python = 'python'
}

Push-Location $projectRoot
try {
    & $python -m PyInstaller --noconfirm --clean --onedir --windowed --name $appName main.py
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE."
    }

    & (Join-Path $PSScriptRoot 'build-signing-pin-helper.ps1') -OutputDirectory $appDirectory
    if ($LASTEXITCODE -ne 0) {
        throw "Signing PIN helper build failed with exit code $LASTEXITCODE."
    }

    New-Item -ItemType Directory -Force $releaseDirectory | Out-Null
    if (Test-Path $archivePath) {
        Remove-Item -LiteralPath $archivePath
    }
    Compress-Archive -Path $appDirectory -DestinationPath $archivePath -CompressionLevel Optimal
    Write-Host "Release archive created: $archivePath"
}
finally {
    Pop-Location
}
