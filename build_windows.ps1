$ErrorActionPreference = "Stop"
if (Test-Path .\build) { Remove-Item .\build -Recurse -Force }
if (Test-Path .\dist\StreamDoctor) { Remove-Item .\dist\StreamDoctor -Recurse -Force }
if (Test-Path .\StreamDoctor.spec) { Remove-Item .\StreamDoctor.spec -Force }
python -m PyInstaller --noconfirm --clean --windowed --name StreamDoctor --add-data "stream_doctor/theme.qss;stream_doctor" launcher.py
Write-Host "Portable executable created at dist\StreamDoctor\StreamDoctor.exe"
Write-Host "Compile installer.iss with Inno Setup 6 to create the standard Windows installer."
