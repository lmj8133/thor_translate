#!/usr/bin/env bash
# Deploy the current repo state to the Thor's Termux proxy and restart it.
#
# Usage (from anywhere - WSL, macOS or Linux - Thor connected via adb):
#     bash tools/deploy_thor.sh [path-to-adb]
#
# Pushes server/ + glossaries/ to the on-device staging dir, syncs them into
# the Termux home, restarts the proxy headlessly, and runs the on-device
# health check (one real translation through the cloud chain).
set -euo pipefail

# Explicit argument wins, then $ADB, then the WSL-era checkout path (kept so
# existing setups keep working), then whatever is on PATH (Homebrew on macOS).
default_adb() {
    if [ -x "$HOME/thor-work/platform-tools/adb" ]; then
        echo "$HOME/thor-work/platform-tools/adb"
    else
        command -v adb || echo adb
    fi
}
ADB="${1:-${ADB:-$(default_adb)}}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

# Self-heal after a Thor reboot: the wireless-debugging port randomizes,
# so scan-and-reconnect instead of asking the user for the new number.
"$ADB" get-state >/dev/null 2>&1 || ADB="$ADB" bash "$REPO/tools/connect_thor.sh"

# Termux environment prefix for headless run-as execution. $-signs stay
# literal here (single quotes) and are expanded by the on-device shell.
RUNAS_ENV='export PREFIX=/data/data/com.termux/files/usr; export HOME=/data/data/com.termux/files/home; export PATH=$PREFIX/bin; export TMPDIR=$PREFIX/tmp'

echo "== push to staging =="
"$ADB" shell mkdir -p /sdcard/thor-proxy
"$ADB" push server /sdcard/thor-proxy/ | tail -1
"$ADB" push glossaries /sdcard/thor-proxy/ | tail -1

echo "== sync into Termux home =="
"$ADB" shell "run-as com.termux sh -c 'cp -r /storage/emulated/0/thor-proxy/server /storage/emulated/0/thor-proxy/glossaries files/home/thor-proxy/ && chmod +x files/home/thor-proxy/server/thor/*.sh && cp files/home/thor-proxy/server/thor/boot-start.sh files/home/.termux/boot/boot-start.sh && chmod +x files/home/.termux/boot/boot-start.sh && echo synced'"

echo "== restart proxy =="
# A headless (run-as) proxy is only safe from Android's empty-process reaper
# while TermuxService is alive - open the app to create a session if needed.
if ! "$ADB" shell "dumpsys activity services com.termux 2>/dev/null" | grep -q ServiceRecord; then
    echo "TermuxService not running - opening the Termux app to pin it"
    "$ADB" shell monkey -p com.termux 1 >/dev/null 2>&1
    sleep 3
fi
# PID parsing happens on the host side - nesting awk through three shell
# layers is how quoting bugs are born.
PID="$("$ADB" shell "run-as com.termux sh -c 'ps -ef'" | grep '[u]vicorn' | awk '{print $2}' | head -1 || true)"
if [ -n "$PID" ]; then
    "$ADB" shell "run-as com.termux sh -c 'kill $PID'"
    sleep 2
fi
# Same glossary semantics as run-proxy.sh: picker-selected per request;
# a default table only if env.sh exports GLOSSARY_PATH.
# Log is appended, not truncated: a restart mid-investigation must not
# destroy the TRACE lines being diagnosed.
# LOG_FILE mirrors run-proxy.sh: this path bypasses it, so the rotating
# handler has to be set here too or a deploy-started proxy logs unbounded.
"$ADB" shell "run-as com.termux sh -c '$RUNAS_ENV; cd \$HOME/thor-proxy; . ./env.sh; export LOG_FILE=\${LOG_FILE:-\$HOME/thor-proxy/proxy-app.log}; nohup uvicorn server.proxy.main:app --host 127.0.0.1 --port 8000 >> proxy.log 2>&1 & echo relaunched'"

echo "== health check =="
"$ADB" shell "run-as com.termux sh -c '$RUNAS_ENV; python /sdcard/thor-proxy/healthcheck.py'"
