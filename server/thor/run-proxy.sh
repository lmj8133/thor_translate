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

# Keep the CPU awake while the screen is off; released on `termux-wake-unlock`.
termux-wake-lock

export GLOSSARY_PATH="${GLOSSARY_PATH:-glossaries/pokemon-oras.txt}"
# 127.0.0.1: reachable by PlayTranslate on this device, invisible to the LAN.
exec uvicorn server.proxy.main:app --host 127.0.0.1 --port 8000
