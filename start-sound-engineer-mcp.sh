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
  echo "[engineer] creating local venv with $PY" >&2
  "$PY" -m venv "$VENV"
fi

WANT="$(shasum -a 256 requirements.txt | cut -d' ' -f1)"
HAVE="$(cat "$STAMP" 2>/dev/null || true)"
if [ "$WANT" != "$HAVE" ]; then
  echo "[engineer] installing dependencies locally" >&2
  "$VENV/bin/python" -m pip install -q --upgrade pip
  "$VENV/bin/python" -m pip install -q -r requirements.txt
  echo "$WANT" > "$STAMP"
fi

RUN=".run"; mkdir -p "$RUN"; PIDFILE="$RUN/engineer.pid"
is_running() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; }

case "${1:-}" in
  --status)
    if is_running; then echo "[engineer] running (pid $(cat "$PIDFILE"))"; else echo "[engineer] not running"; fi
    exit 0 ;;
  --stop)
    if is_running; then
      kill "$(cat "$PIDFILE")" 2>/dev/null && echo "[engineer] stopped (pid $(cat "$PIDFILE"))"
    else
      echo "[engineer] not running"
    fi
    rm -f "$PIDFILE"; exit 0 ;;
  --restart)
    if is_running; then kill "$(cat "$PIDFILE")" 2>/dev/null; sleep 1; rm -f "$PIDFILE"; fi
    shift || true ;;
  --test)  shift
           set -o pipefail
           OUT="$(mktemp)"
           trap 'rm -f "$OUT"' EXIT
           FAILED=0
           for f in tests/test_*.py; do
             echo "[engineer] $f" >&2
             if [ "$f" = "tests/test_smoke.py" ]; then
               "$VENV/bin/python" "$f" "$@" 2>&1 | tee -a "$OUT" || FAILED=1
             else
               "$VENV/bin/python" "$f" 2>&1 | tee -a "$OUT" || FAILED=1
             fi
           done
           RAN="$(grep -c '^  ok  ' "$OUT" || true)"
           DEFINED="$(grep -h '^def test_' tests/test_*.py | wc -l | tr -d ' ')"
           if [ "$RAN" -ne "$DEFINED" ]; then
             echo "[engineer] test count mismatch: defined=$DEFINED executed=$RAN" >&2
             exit 1
           fi
           if [ "$FAILED" -ne 0 ]; then
             echo "[engineer] test failure(s) above" >&2
             exit 1
           fi
           echo "[engineer] $RAN tests passed" >&2
           exit 0 ;;
esac

if [ ! -f .env ]; then
  cp .env.example .env
  echo "[engineer] .env created — put your JEV_API_KEY in it, then run again" >&2
fi

echo $$ > "$PIDFILE"
case "${1:-}" in
  --stdio) shift; exec "$VENV/bin/python" server/engineer_server.py "$@" ;;
  --http)  shift; exec "$VENV/bin/python" server/engineer_server.py --http "$@" ;;
  *)       exec "$VENV/bin/python" server/engineer_server.py --http "$@" ;;
esac
