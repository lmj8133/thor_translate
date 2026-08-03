#!/data/data/com.termux/files/usr/bin/sh
# Termux:Boot hook - starts the proxy after device boot; output goes to a log.
# Deliberately NO wake lock: translation only happens while the screen is on
# (the capture app needs it), so the proxy can freeze with the device during
# doze and thaw instantly on wake - idle battery cost stays at zero.
"$HOME/thor-proxy/server/thor/run-proxy.sh" > "$HOME/thor-proxy/proxy.log" 2>&1 &
