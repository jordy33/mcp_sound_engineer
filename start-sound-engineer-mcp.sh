#!/usr/bin/env bash
# Single entry point. Everything this project needs lives in this folder:
#   .venv/   local virtualenv (created on first run)
#   .env     local secrets (copied from .env.example on first run)
# Nothing is installed globally, nothing is written outside this directory.
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
VENV=".venv"
STAMP="$VENV/.deps.sha"

if [ ! -d "$VENV" ]; then
  echo "[engineer] creating local venv with $PY"
  "$PY" -m venv "$VENV"
fi

WANT="$(shasum -a 256 requirements.txt | cut -d' ' -f1)"
HAVE="$(cat "$STAMP" 2>/dev/null || true)"
if [ "$WANT" != "$HAVE" ]; then
  echo "[engineer] installing dependencies locally"
  "$VENV/bin/python" -m pip install -q --upgrade pip
  "$VENV/bin/python" -m pip install -q -r requirements.txt
  echo "$WANT" > "$STAMP"
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "[engineer] .env created — put your JEV_API_KEY in it, then run again"
  exit 1
fi

case "${1:-}" in
  --test)  shift; exec "$VENV/bin/python" tests/test_smoke.py "$@" ;;
  --http)  shift; exec "$VENV/bin/python" server/engineer_server.py --http "$@" ;;
  *)       exec "$VENV/bin/python" server/engineer_server.py "$@" ;;
esac
