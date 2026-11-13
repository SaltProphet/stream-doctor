param([string]$InnoCompiler)

$ErrorActionPreference = "Stop"
Push-Location $PSScriptRoot
try {
    python -m PyInstaller --noconfirm --clean --windowed --name StreamDoctor --add-data "stream_doctor/theme.qss;stream_doctor" launcher.py
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed ($LASTEXITCODE)." }
    if (-not (Test-Path .\dist\StreamDoctor\StreamDoctor.exe)) { throw "StreamDoctor.exe was not produced." }
    if ($InnoCompiler) {
        & $InnoCompiler .\installer.iss
        if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed ($LASTEXITCODE)." }
    }
} finally {
    Pop-Location
}
