#!/usr/bin/env bash
# Reconnect wireless adb to the Thor - self-healing across reboots AND
# network changes (home LAN, iPhone hotspot, anywhere).
#
# The wireless-debugging CONNECT port randomizes on every reboot and the IP
# changes per network, but the PAIRING persists - so discovering the device
# and connecting is enough; no re-pairing, no reading numbers off the screen.
#
# Discovery order: $THOR_IP -> last successful IP (cached) -> home default
# -> ping-sweep of the local subnets (the Windows host's under WSL, this
# machine's own on macOS/Linux).
#
# Requires: Wireless debugging toggled ON on the Thor (it may reset to off
# after a reboot - add its Quick Settings tile for a one-tap re-enable).
set -euo pipefail

# Prefer an explicit $ADB, then the WSL-era checkout path (kept so existing
# setups keep working), then whatever is on PATH (Homebrew on macOS).
default_adb() {
    if [ -x "$HOME/thor-work/platform-tools/adb" ]; then
        echo "$HOME/thor-work/platform-tools/adb"
    else
        command -v adb || echo adb
    fi
}
ADB="${ADB:-$(default_adb)}"
HOME_IP="192.168.1.104"
IP_CACHE="$HOME/.cache/thor-ip"

connected() { "$ADB" devices | grep -q "device$"; }

try_host() {
    local ip="$1"
    echo "Scanning $ip for the wireless-debugging port..."
    local ports
    ports="$(python3 - "$ip" <<'EOF'
import socket
import sys
from concurrent.futures import ThreadPoolExecutor

ip = sys.argv[1]

def probe(port):
    sock = socket.socket()
    sock.settimeout(0.3)
    try:
        sock.connect((ip, port))
        return port
    except OSError:
        return None
    finally:
        sock.close()

with ThreadPoolExecutor(200) as pool:
    for port in pool.map(probe, range(30000, 50000)):
        if port:
            print(port)
EOF
)"
    local port
    for port in $ports; do
        "$ADB" connect "$ip:$port" >/dev/null 2>&1 || true
        if connected; then
            echo "Connected: $ip:$port"
            mkdir -p "$(dirname "$IP_CACHE")"
            echo "$ip" > "$IP_CACHE"
            return 0
        fi
        "$ADB" disconnect "$ip:$port" >/dev/null 2>&1 || true
    done
    return 1
}

# /24 prefixes worth sweeping, one per line. Under WSL the Thor sits on the
# Windows host's networks, not in WSL's own NAT subnet; on macOS/Linux the
# interfaces of this machine are the ones that matter.
local_prefixes() {
    if [ -e /mnt/c/Windows/System32/ipconfig.exe ]; then
        # ipconfig output may be non-UTF8 - filter roughly.
        local wsl_gw
        wsl_gw="$(ip route | awk '/^default/ {print $3; exit}')"
        /mnt/c/Windows/System32/ipconfig.exe 2>/dev/null \
            | { grep -oE '([0-9]{1,3}\.){3}[0-9]{1,3}' || true; } \
            | { grep -E '^(10\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.)' || true; } \
            | sed 's/\.[0-9]*$//' | sort -u \
            | { grep -vx "${wsl_gw%.*}" || true; }
    else
        # macOS `ifconfig` and Linux `ip -4 addr` both yield "inet <addr>".
        { ifconfig 2>/dev/null || ip -4 addr 2>/dev/null; } \
            | awk '/[ \t]inet /{print $2}' \
            | sed 's#/.*##' \
            | { grep -E '^(10\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.)' || true; } \
            | sed 's/\.[0-9]*$//' | sort -u
    fi
}

# macOS ping -W is milliseconds, Linux ping -W is seconds; -t is a seconds
# deadline on macOS but a TTL on Linux. Pick the flag that means "give up
# after ~1s" on the platform we are actually on.
ping_once() {
    if [ "$(uname -s)" = "Darwin" ]; then
        ping -c1 -t1 "$1" >/dev/null 2>&1
    else
        ping -c1 -W1 "$1" >/dev/null 2>&1
    fi
}

sweep_subnets() {
    local prefix i
    local_prefixes | while read -r prefix; do
        [ -n "$prefix" ] || continue
        echo "Ping-sweeping $prefix.0/24..." >&2
        for i in $(seq 1 254); do
            (ping_once "$prefix.$i" && echo "$prefix.$i") &
        done
        wait 2>/dev/null
    done
}

if connected; then
    echo "adb already connected"
    exit 0
fi
"$ADB" disconnect >/dev/null 2>&1 || true

# Layer 1-3: explicit env var, last success, home default.
CANDIDATES=""
[ -n "${THOR_IP:-}" ] && CANDIDATES="$THOR_IP"
[ -f "$IP_CACHE" ] && CANDIDATES="$CANDIDATES $(cat "$IP_CACHE")"
CANDIDATES="$CANDIDATES $HOME_IP"
for ip in $(echo "$CANDIDATES" | tr ' ' '\n' | awk 'NF && !seen[$0]++'); do
    try_host "$ip" && exit 0
done

# Layer 4: discover live hosts on the local subnets and try each.
echo "Known IPs failed - discovering hosts on the local network..."
for ip in $(sweep_subnets); do
    try_host "$ip" && exit 0
done

echo "Could not find the Thor - checklist:" >&2
echo "  1. Thor 的「無線偵錯」開了嗎？（重開機後常會自動關閉）" >&2
echo "  2. Thor 和這台電腦連的是同一個網路嗎？" >&2
echo "  3. 都確認了還不行：Thor 無線偵錯頁上的 IP 用 THOR_IP=<ip> 帶入重試" >&2
exit 1
