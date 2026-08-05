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

# No autostart hook on purpose: Termux:Boot fires before Wi-Fi is up, so a
# boot-started proxy is reachable but cannot translate, and PlayTranslate's
# first failed request puts the service into its own ~30-minute cooldown.
# Starting it by hand (after the network is up) is both simpler and more
# reliable - see README-thor.md.
rm -f "$HOME/.termux/boot/boot-start.sh"

echo ""
echo "Setup done."
if [ ! -f "$DEST/env.sh" ]; then
    echo "NEXT STEP - create $DEST/env.sh with your API key:"
    echo "  echo 'export GEMINI_API_KEY=\"AIza...\"' > $DEST/env.sh"
fi
echo "Then start the proxy with:"
echo "  bash $DEST/server/thor/run-proxy.sh"
