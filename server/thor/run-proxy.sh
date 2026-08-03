#!/data/data/com.termux/files/usr/bin/bash
# Launch the translation proxy on-device, bound to loopback only.
set -e

DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$DIR"

if [ -f env.sh ]; then
    . ./env.sh
fi
if [ -z "$GEMINI_API_KEY" ]; then
    echo "GEMINI_API_KEY is not set. Create $DIR/env.sh with:" >&2
    echo "  export GEMINI_API_KEY=\"AIza...\"" >&2
    exit 1
fi

# No wake lock on purpose: translation only happens with the screen on, so
# the proxy sleeps with the device and idle battery cost is zero. If a
# screen-off use case ever appears, run `termux-wake-lock` manually.

# Glossary selection is per-request via PT's model picker; a default table
# is opt-in only (export GLOSSARY_PATH in env.sh if you want one).
# 127.0.0.1: reachable by PlayTranslate on this device, invisible to the LAN.
exec uvicorn server.proxy.main:app --host 127.0.0.1 --port 8000
