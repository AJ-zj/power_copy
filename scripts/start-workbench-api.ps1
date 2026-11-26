param(
    [string]$ProjectRoot,
    [string]$Python,
    [int]$Port = 8000
)

$ErrorActionPreference = 'Stop'
$workspace = Split-Path -Parent $PSScriptRoot
if (-not $ProjectRoot) {
    $ProjectRoot = $env:POWERCOPY_PROJECT_ROOT
}
if (-not $ProjectRoot) {
    $ProjectRoot = $workspace
    if (-not (Test-Path (Join-Path $ProjectRoot 'powercopy/core/trace.py'))) {
        $ProjectRoot = Join-Path $workspace '../../PowerCopy-V2'
    }
}
$env:POWERCOPY_PROJECT_ROOT = (Resolve-Path -LiteralPath $ProjectRoot).Path
if (-not $Python) {
    $Python = Join-Path $env:POWERCOPY_PROJECT_ROOT '.venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $Python)) {
        $Python = (Get-Command python.exe -ErrorAction Stop).Source
    }
}
$localDependencies = Join-Path $workspace '.workbench-deps'
if (Test-Path -LiteralPath $localDependencies) {
    $env:PYTHONPATH = "$localDependencies;$env:PYTHONPATH"
}
$env:PYTHONDONTWRITEBYTECODE = '1'
Push-Location -LiteralPath $workspace
try {
    & $Python -m uvicorn workbench.main:app --host 127.0.0.1 --port $Port
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
