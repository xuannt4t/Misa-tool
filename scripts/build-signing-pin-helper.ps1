param(
    [string]$OutputDirectory = "dist"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    $python = 'python'
}

Push-Location $projectRoot
try {
    & $python -m PyInstaller --noconfirm --clean --onefile --windowed --uac-admin `
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
