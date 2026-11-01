#!/bin/sh
set -eu
cd "$(dirname "$0")"
TRACE_PYTHON_BIN="${TRACE_PYTHON:-python3}"
exec "$TRACE_PYTHON_BIN" boutique_server.py
