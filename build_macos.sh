#!/bin/bash
# One-step build + deploy for the macOS build: rebuilds the self-contained
# app (jukebox_macos.spec), reinstalls it to /Applications, rebuilds the
# DMG installer, and copies that DMG into the Desktop delivery folder.
# Run this after any change to app.py/desktop_macos.py/config.py or
# anything under static/ -- a plain file copy isn't enough once the change
# needs to reach the compiled app; see jukebox_macos.spec's own header
# comment for why this exists as a separate bundle from the source tree.
#
# Usage: ./build_macos.sh
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

APP_NAME="Notorious BPM.app"
VOL_NAME="Notorious B.P.M."
DMG_NAME="Notorious BPM.dmg"
DELIVERY_DIR="$HOME/Desktop/Notorious B.P.M."

echo "== Building app bundle with PyInstaller =="
rm -rf dist-macos-new build-macos-new
/usr/bin/python3 -m PyInstaller jukebox_macos.spec --distpath dist-macos-new --workpath build-macos-new --noconfirm

echo "== Installing to /Applications =="
pkill -9 -f "Notorious BPM" 2>/dev/null || true
sleep 1
PIDS=$(lsof -ti:5151 2>/dev/null || true)
if [ -n "$PIDS" ]; then kill -9 $PIDS; fi
sleep 1
rm -rf "/Applications/$APP_NAME"
cp -R "dist-macos-new/$APP_NAME" "/Applications/$APP_NAME"

echo "== Updating repo reference copy =="
rm -rf "dist-macos/$APP_NAME"
cp -R "dist-macos-new/$APP_NAME" "dist-macos/$APP_NAME"
rm -rf dist-macos-new build-macos-new

echo "== Building DMG installer =="
rm -f "dist-macos/$DMG_NAME"
cp "LICENSE" "dist-macos/LICENSE.txt"
create-dmg \
  --volname "$VOL_NAME" \
  --volicon "assets/AppIcon.icns" \
  --background "assets/dmg_background.png" \
  --window-pos 200 120 \
  --window-size 660 420 \
  --icon-size 110 \
  --icon "$APP_NAME" 180 225 \
  --hide-extension "$APP_NAME" \
  --app-drop-link 480 225 \
  --add-file "LICENSE.txt" "dist-macos/LICENSE.txt" 330 340 \
  --no-internet-enable \
  "dist-macos/$DMG_NAME" \
  "dist-macos/$APP_NAME"

echo "== Copying DMG to delivery folder =="
if [ -d "$DELIVERY_DIR/OSX" ]; then
  cp "dist-macos/$DMG_NAME" "$DELIVERY_DIR/OSX/$DMG_NAME"
fi

echo "== Relaunching app =="
open "/Applications/$APP_NAME"
sleep 3
curl -s -o /dev/null -w "app responding: %{http_code}\n" http://127.0.0.1:5151/api/facets

echo "== Done =="
echo "App:      /Applications/$APP_NAME"
echo "DMG:      $(pwd)/dist-macos/$DMG_NAME"
if [ -d "$DELIVERY_DIR/OSX" ]; then
  echo "Delivery: $DELIVERY_DIR/OSX/$DMG_NAME"
fi
