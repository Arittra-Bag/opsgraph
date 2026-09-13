$ErrorActionPreference = 'Stop'
& py -3.11 -I (Join-Path $PSScriptRoot 'Install.py') install @args
exit $LASTEXITCODE
