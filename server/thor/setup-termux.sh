#!/data/data/com.termux/files/usr/bin/bash
# One-shot Termux setup for the on-device translation proxy.
# Prerequisites: Termux (F-Droid build), `termux-setup-storage` granted,
# and the repo staged at /sdcard/thor-proxy (pushed via adb).
set -e

STAGE=/storage/emulated/0/thor-proxy
DEST="$HOME/thor-proxy"

if [ ! -d "$STAGE/server" ]; then
    echo "Staging dir $STAGE/server not found - push the repo via adb first." >&2
    exit 1
fi

pkg update -y
pkg install -y python
# All pure Python - no compiler needed on Android. Do NOT `pip install
# --upgrade pip` here: Termux forbids it (breaks its python-pip package).
pip install starlette uvicorn httpx opencc-python-reimplemented

mkdir -p "$DEST"
cp -r "$STAGE/server" "$STAGE/glossaries" "$DEST/"
chmod +x "$DEST"/server/thor/*.sh

# Autostart hook - takes effect once the Termux:Boot app is installed
# and has been opened at least once.
mkdir -p "$HOME/.termux/boot"
cp "$DEST/server/thor/boot-start.sh" "$HOME/.termux/boot/boot-start.sh"
chmod +x "$HOME/.termux/boot/boot-start.sh"

echo ""
echo "Setup done."
if [ ! -f "$DEST/env.sh" ]; then
    echo "NEXT STEP - create $DEST/env.sh with your API key:"
    echo "  echo 'export GEMINI_API_KEY=\"AIza...\"' > $DEST/env.sh"
fi
echo "Then start the proxy with:"
echo "  bash $DEST/server/thor/run-proxy.sh"
