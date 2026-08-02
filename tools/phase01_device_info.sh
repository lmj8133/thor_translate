#!/usr/bin/env bash
# Phase 01 step 1: collect AYN Thor device info over adb and print a
# markdown snippet ready to paste into docs/verification-results.md.
#
# Usage:
#   ./tools/phase01_device_info.sh [IP:PORT]
#
#   IP:PORT  optional wireless-debugging address; the script runs
#            `adb connect IP:PORT` first. First-time wireless setup needs
#            pairing on the device: Settings > Developer options >
#            Wireless debugging > Pair device with pairing code, then run
#            `adb pair IP:PAIR_PORT` manually before using this script.
#
# Examples:
#   ./tools/phase01_device_info.sh                 # USB or already-connected device
#   ./tools/phase01_device_info.sh 192.168.1.50:5555
#
# Exit codes: 0 ok, 1 adb not found, 2 no device connected.

set -u

find_adb() {
  if command -v adb >/dev/null 2>&1; then
    echo "adb"
  elif [ -x "$HOME/thor-work/platform-tools/adb" ]; then
    echo "$HOME/thor-work/platform-tools/adb"
  else
    return 1
  fi
}

ADB="$(find_adb)" || { echo "error: adb not found (looked in PATH and ~/thor-work/platform-tools)" >&2; exit 1; }

if [ $# -ge 1 ]; then
  "$ADB" connect "$1" >&2 || true
fi

if ! "$ADB" get-state >/dev/null 2>&1; then
  echo "error: no device connected (enable USB/wireless debugging, check 'adb devices')" >&2
  exit 2
fi

model="$("$ADB" shell getprop ro.product.model | tr -d '\r')"
android="$("$ADB" shell getprop ro.build.version.release | tr -d '\r')"
build="$("$ADB" shell getprop ro.build.display.id | tr -d '\r')"
mem_kb="$("$ADB" shell head -1 /proc/meminfo | awk '{print $2}' | tr -d '\r')"
mem_gb="$(awk "BEGIN {printf \"%.1f\", $mem_kb/1024/1024}")"

# Marketed RAM SKU (8/12/16GB) is the smallest standard size >= MemTotal.
sku="unknown"
for s in 8 12 16 24 32; do
  if awk "BEGIN {exit !($mem_kb <= $s*1024*1024)}"; then sku="${s}GB"; break; fi
done

pt_ver="$("$ADB" shell dumpsys package com.playtranslate 2>/dev/null | grep -m1 versionName | sed 's/.*versionName=//' | tr -d '\r')"
az_ver="$("$ADB" shell dumpsys package org.azahar_emu.azahar 2>/dev/null | grep -m1 versionName | sed 's/.*versionName=//' | tr -d '\r')"

echo "## Device info (paste into docs/verification-results.md)"
echo
echo "| 項目 | 值 |"
echo "|------|-----|"
echo "| Thor SKU / RAM | ${sku} (MemTotal ${mem_gb} GB) |"
echo "| Android 版本 | ${android} (${build}) |"
echo "| Model | ${model} |"
echo "| PlayTranslate 版本 | ${pt_ver:-not installed} |"
echo "| Azahar 版本 | ${az_ver:-not installed} |"
echo
echo "## Displays (identify top/bottom displayId from the dump below)"
echo
echo '```'
"$ADB" shell dumpsys display | grep -E "mDisplayId|DisplayDeviceInfo|uniqueId" | sed 's/^[[:space:]]*//' | head -40
echo '```'
