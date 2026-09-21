param(
    [int]$Port = 8765,
    [string]$HostAddress = '0.0.0.0',
    [double]$Interval = 2,
    [double]$StallMinutes = 10,
    [string]$ProjectRoot = ''
)

$ErrorActionPreference = 'Stop'
if (-not $ProjectRoot) {
    $ProjectRoot = if ((Split-Path (Split-Path $PSScriptRoot) -Leaf) -eq 'tools') { Join-Path $PSScriptRoot '..\..' } else { $PSScriptRoot }
}
$projectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$server = Join-Path $PSScriptRoot 'research_server.py'

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw '未找到 Python，无法运行 Research Sentinel。'
}

& python $server --project $projectRoot --host $HostAddress --port $Port --interval $Interval --stall-minutes $StallMinutes
exit $LASTEXITCODE
