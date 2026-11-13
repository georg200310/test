#!/bin/sh
set -eu

TOKEN_ENTRY="/opt/bgutil-ytdlp-pot-provider/server/build/main.js"
if [ ! -f "$TOKEN_ENTRY" ]; then
  echo "BgUtils PO-Token service not found at $TOKEN_ENTRY" >&2
  exit 1
fi

node "$TOKEN_ENTRY" --port 4416 &
TOKEN_PID=$!
APP_PID=""
cleanup() {
  if [ -n "$APP_PID" ]; then kill "$APP_PID" 2>/dev/null || true; fi
  kill "$TOKEN_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

/opt/venv/bin/python - <<'PY'
import socket, sys, time
for attempt in range(80):
    try:
        with socket.create_connection(("127.0.0.1", 4416), timeout=0.25):
            print("BgUtils PO-Token service ready on 127.0.0.1:4416", flush=True)
            break
    except OSError:
        time.sleep(0.25)
else:
    print("BgUtils PO-Token service did not start", file=sys.stderr, flush=True)
    sys.exit(1)
PY

/opt/venv/bin/uvicorn app:app --host 0.0.0.0 --port "${PORT:-10000}" --workers 1 --timeout-keep-alive 75 &
APP_PID=$!
set +e
wait "$APP_PID"
APP_STATUS=$?
kill "$TOKEN_PID" 2>/dev/null || true
wait "$TOKEN_PID" 2>/dev/null || true
exit "$APP_STATUS"
