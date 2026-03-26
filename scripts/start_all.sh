#!/usr/bin/env bash
set -euo pipefail

python scripts/run_worker.py &
worker_pid=$!

cleanup() {
  kill "$worker_pid" 2>/dev/null || true
}

trap cleanup EXIT INT TERM

exec python scripts/run_web.py
