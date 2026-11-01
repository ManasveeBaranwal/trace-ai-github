#!/bin/sh
set -eu
cd "$(dirname "$0")"
TRACE_PYTHON_BIN="${TRACE_PYTHON:-python3}"
export TRACE_REQUIRE_LOGIN="${TRACE_REQUIRE_LOGIN:-1}"
exec "$TRACE_PYTHON_BIN" -m uvicorn app:app --host 127.0.0.1 --port "${TRACE_PORT:-8002}"
