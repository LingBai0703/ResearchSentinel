param([switch]$Clean)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if ($Clean) {
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue build, dist
}
python -m pip install -r requirements-build.txt
python -m PyInstaller --noconfirm --clean ResearchSentinel.spec
Write-Host "Built: $PSScriptRoot\dist\ResearchSentinel.exe"
