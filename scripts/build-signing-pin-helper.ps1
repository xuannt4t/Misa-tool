param(
    [string]$OutputDirectory = "dist"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot

Push-Location $projectRoot
try {
    python -m PyInstaller --noconfirm --clean --onedir --windowed `
        --name "MISA Signing PIN Helper" `
        --distpath $OutputDirectory `
        tools/signing_pin_helper.py
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE."
    }
}
finally {
    Pop-Location
}
