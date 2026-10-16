#!/bin/sh
set -e

SCRIPT_DIR="$(cd "$(dirname "${0}")" && pwd)"
if [ -f "${SCRIPT_DIR}/unlock.py" ]; then
  exec python3 "${SCRIPT_DIR}/unlock.py" "${@}"
elif [ -f "/usr/local/bin/unlock" ]; then
  exec python3 /usr/local/bin/unlock "${@}"
else
  echo "Error: unlock.py not found" >&2
  exit 1
fi
