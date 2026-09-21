#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -n "${RESEARCH_SENTINEL_PROJECT:-}" ]; then
    PROJECT_ROOT=$RESEARCH_SENTINEL_PROJECT
elif [ "$(basename "$(dirname "$SCRIPT_DIR")")" = "tools" ]; then
    PROJECT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
else
    PROJECT_ROOT=$SCRIPT_DIR
fi
PYTHON_BIN=${PYTHON_BIN:-python3}
HOST_ADDRESS=${RESEARCH_SENTINEL_HOST:-0.0.0.0}
PORT=${RESEARCH_SENTINEL_PORT:-8765}
INTERVAL=${RESEARCH_SENTINEL_INTERVAL:-2}
STALL_MINUTES=${RESEARCH_SENTINEL_STALL_MINUTES:-10}

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "Python 3 was not found: $PYTHON_BIN" >&2
    exit 1
fi

exec "$PYTHON_BIN" "$SCRIPT_DIR/research_server.py" \
    --project "$PROJECT_ROOT" \
    --host "$HOST_ADDRESS" \
    --port "$PORT" \
    --interval "$INTERVAL" \
    --stall-minutes "$STALL_MINUTES" \
    "$@"
