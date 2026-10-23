$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'

Set-Location $projectRoot

if (-not (Test-Path $venvPython)) {
    Write-Host 'Creating the project Python environment...'
    & py -m venv .venv
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    Write-Host 'Installing project dependencies...'
    & $venvPython -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

& $venvPython app.py
exit $LASTEXITCODE
