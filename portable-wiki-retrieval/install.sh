#!/usr/bin/env sh
set -eu
export PYTHONUTF8=1
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CLIENT=${1:-all}
PROJECT=${2:-"$PWD"}
python3 "$SCRIPT_DIR/scripts/install.py" --client "$CLIENT" --project "$PROJECT"
