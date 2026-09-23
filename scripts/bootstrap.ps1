$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
python -m pip install --user uv==0.12.17
if ($LASTEXITCODE -ne 0) { throw 'Falha ao instalar uv.' }
python -m uv sync --locked
if ($LASTEXITCODE -ne 0) { throw 'Falha ao instalar as dependências.' }
python -m uv run tcc-pipeline doctor
if ($LASTEXITCODE -ne 0) { throw 'Verifique as pendências do diagnóstico.' }
Write-Host 'Pronto. Execute: python -m uv run tcc-pipeline demo'
