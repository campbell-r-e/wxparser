#!/usr/bin/env bash
# Pull-based CD for the LAN-only weather box (cloud CI runners can't reach it).
#
# Fast-forwards main, re-runs the FULL test suite in a persistent venv, and
# restarts the services only if green — rolling the working tree back to the
# previous commit on failure, so a bad push never takes the capture box down.
# Driven by wxparser-deploy.timer. Append-only log at $LOG.
set -uo pipefail

REPO=/home/creed/wxparser
VENV=/home/creed/wxparser-testenv
LOG=/home/creed/wxparser-deploy.log
cd "$REPO" || exit 1

git fetch -q origin main || { echo "$(date -Is) fetch failed" >>"$LOG"; exit 1; }
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)
[ "$LOCAL" = "$REMOTE" ] && exit 0           # nothing new -> done

echo "$(date -Is) new commit ${REMOTE:0:8} (was ${LOCAL:0:8}) — testing" >>"$LOG"

# persistent test venv: runtime deps from system site-packages + pytest/coverage
if [ ! -x "$VENV/bin/python" ]; then
    python3 -m venv --system-site-packages "$VENV"
    "$VENV/bin/pip" -q install pytest coverage
fi
PY="$VENV/bin/python"

git pull -q --ff-only origin main

# make sure the test database exists (idempotent)
"$PY" - <<'PYEOF' 2>/dev/null || true
import pg8000.native as p
try:
    p.Connection(user="wxparser", host="127.0.0.1", database="wxparser").run(
        "CREATE DATABASE wxparser_test")
except Exception:
    pass
PYEOF

if "$PY" -m coverage run -m pytest -q >>"$LOG" 2>&1 && "$PY" -m coverage report >>"$LOG" 2>&1; then
    sudo systemctl restart wxparser wxparser-api
    echo "$(date -Is) DEPLOYED ${REMOTE:0:8}" >>"$LOG"
else
    git reset --hard "$LOCAL" >>"$LOG" 2>&1
    echo "$(date -Is) TESTS FAILED — rolled back to ${LOCAL:0:8}, services unchanged" >>"$LOG"
    exit 1
fi
