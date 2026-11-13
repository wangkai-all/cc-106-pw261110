#!/usr/bin/env bash
# Reset the dev database and run the full API end-to-end suite.
# Safe against self-matching pkill: it kills the uvicorn parent by port, not pattern.
set -euo pipefail
cd "$(dirname "$0")/../.."

PORT=8077
echo "== stopping any uvicorn on :$PORT =="
.venv/bin/python - "$PORT" <<'PY'
import glob, os, signal, sys
port = int(sys.argv[1]); inode = None
for path in ("/proc/net/tcp", "/proc/net/tcp6"):
    try:
        lines = open(path).read().splitlines()[1:]
    except OSError:
        continue
    for ln in lines:
        parts = ln.split()
        local = parts[1]; state = parts[3]; ino = parts[9]
        ip, p = local.rsplit(":", 1)
        if int(p, 16) == port and state == "0A":  # LISTEN
            inode = ino
if not inode:
    print("no listener"); sys.exit(0)
self = os.getpid()
for fd in glob.glob("/proc/[0-9]*/fd/*"):
    try:
        link = os.readlink(fd)
    except OSError:
        continue
    if link == f"socket:[{inode}]":
        pid = int(fd.split("/")[2])
        if pid != self:
            print(f"killing pid {pid}")
            os.kill(pid, signal.SIGTERM)
PY
sleep 1

echo "== resetting database =="
rm -f data/measurements.sqlite3

echo "== starting uvicorn =="
.venv/bin/python -m uvicorn backend.app.main:app \
  --host 127.0.0.1 --port "$PORT" > /tmp/e2e-uvicorn.log 2>&1 &
SRV=$!
trap 'kill "$SRV" 2>/dev/null || true' EXIT

for _ in $(seq 1 30); do
  if curl -s -o /dev/null "http://127.0.0.1:${PORT}/api/files"; then break; fi
  sleep 0.5
done

echo "== running e2e checks =="
.venv/bin/python -I backend/scripts/e2e_check.py
