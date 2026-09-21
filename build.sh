#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR"
python3 -m pip install -r requirements-build.txt
python3 -m PyInstaller --noconfirm --clean ResearchSentinel.spec
echo "Built: $SCRIPT_DIR/dist/ResearchSentinel"
