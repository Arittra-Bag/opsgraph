#!/bin/sh
set -eu
cd -- "$(dirname -- "$0")"
if command -v python3.11 >/dev/null 2>&1; then
  exec python3.11 -I Install.py launch "$@"
fi
exec python3 -I Install.py launch "$@"
