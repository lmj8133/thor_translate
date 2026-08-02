#!/usr/bin/env bash
# Launch an app on the AYN Thor's bottom screen (displayId 4) via adb.
#
# Usage:
#   ./tools/launch_on_bottom.sh [package/activity]
#
# Examples:
#   ./tools/launch_on_bottom.sh                                  # PlayTranslate (default)
#   ./tools/launch_on_bottom.sh org.azahar_emu.azahar            # any package (resolves main activity)
#
# Exit codes: 0 ok, 1 adb not found, 2 no device connected.

set -u

BOTTOM_DISPLAY_ID=4
TARGET="${1:-com.playtranslate/.MainActivity}"

if command -v adb >/dev/null 2>&1; then
  ADB="adb"
elif [ -x "$HOME/thor-work/platform-tools/adb" ]; then
  ADB="$HOME/thor-work/platform-tools/adb"
else
  echo "error: adb not found (looked in PATH and ~/thor-work/platform-tools)" >&2
  exit 1
fi

if ! "$ADB" get-state >/dev/null 2>&1; then
  echo "error: no device connected (check 'adb devices')" >&2
  exit 2
fi

# Package without activity: resolve its launcher activity first.
if [[ "$TARGET" != */* ]]; then
  RESOLVED="$("$ADB" shell cmd package resolve-activity --brief "$TARGET" 2>/dev/null | tail -1 | tr -d '\r')"
  if [[ "$RESOLVED" != */* ]]; then
    echo "error: cannot resolve launcher activity for package '$TARGET'" >&2
    exit 2
  fi
  TARGET="$RESOLVED"
fi

exec "$ADB" shell am start --display "$BOTTOM_DISPLAY_ID" -n "$TARGET"
