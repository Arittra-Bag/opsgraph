$ErrorActionPreference = 'Stop'
& py -3.11 -I (Join-Path $PSScriptRoot 'Install.py') uninstall @args
exit $LASTEXITCODE
