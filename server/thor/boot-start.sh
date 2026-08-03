#!/data/data/com.termux/files/usr/bin/sh
# Termux:Boot hook - starts the proxy after device boot; output goes to a log.
termux-wake-lock
"$HOME/thor-proxy/server/thor/run-proxy.sh" > "$HOME/thor-proxy/proxy.log" 2>&1 &
