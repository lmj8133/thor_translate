#!/usr/bin/env bash
# Deploy the current repo state to the Thor's Termux proxy and restart it.
#
# Usage (from anywhere, WSL side, Thor connected via adb):
#     bash tools/deploy_thor.sh [path-to-adb]
#
# Pushes server/ + glossaries/ to the on-device staging dir, syncs them into
# the Termux home, restarts the proxy headlessly, and runs the on-device
# health check (one real translation through the cloud chain).
set -euo pipefail

ADB="${1:-$HOME/thor-work/platform-tools/adb}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

"$ADB" get-state >/dev/null || { echo "No adb device - connect the Thor first" >&2; exit 1; }

# Termux environment prefix for headless run-as execution. $-signs stay
# literal here (single quotes) and are expanded by the on-device shell.
RUNAS_ENV='export PREFIX=/data/data/com.termux/files/usr; export HOME=/data/data/com.termux/files/home; export PATH=$PREFIX/bin; export TMPDIR=$PREFIX/tmp'

echo "== push to staging =="
"$ADB" shell mkdir -p /sdcard/thor-proxy
"$ADB" push server /sdcard/thor-proxy/ | tail -1
"$ADB" push glossaries /sdcard/thor-proxy/ | tail -1

echo "== sync into Termux home =="
"$ADB" shell "run-as com.termux sh -c 'cp -r /storage/emulated/0/thor-proxy/server /storage/emulated/0/thor-proxy/glossaries files/home/thor-proxy/ && chmod +x files/home/thor-proxy/server/thor/*.sh && echo synced'"

echo "== restart proxy =="
# PID parsing happens on the WSL side - nesting awk through three shell
# layers is how quoting bugs are born.
PID="$("$ADB" shell "run-as com.termux sh -c 'ps -ef'" | grep '[u]vicorn' | awk '{print $2}' | head -1 || true)"
if [ -n "$PID" ]; then
    "$ADB" shell "run-as com.termux sh -c 'kill $PID'"
    sleep 2
fi
"$ADB" shell "run-as com.termux sh -c '$RUNAS_ENV; cd \$HOME/thor-proxy; . ./env.sh; export GLOSSARY_PATH=glossaries/pokemon-oras.txt; nohup uvicorn server.proxy.main:app --host 127.0.0.1 --port 8000 > proxy.log 2>&1 & echo relaunched'"

echo "== health check =="
"$ADB" shell "run-as com.termux sh -c '$RUNAS_ENV; python /sdcard/thor-proxy/healthcheck.py'"
