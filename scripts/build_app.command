#!/bin/bash
# Build MooMooTrader.app + a drag-to-Applications DMG.
#
#   scripts/build_app.command                  → dist/MooMooTrader.app + dist/MooMooTrader.dmg
#   CODESIGN_ID="Developer ID Application: …" scripts/build_app.command
#                                              → same, signed for distribution
#
# Ship checklist (before publishing a release build):
#   1. Notarize (needs an Apple Developer ID, $99/yr):
#        xcrun notarytool submit dist/MooMooTrader.dmg --keychain-profile mmt --wait
#        xcrun stapler staple dist/MooMooTrader.dmg
#      Without notarization, users must right-click → Open on first launch.
set -euo pipefail
cd "$(dirname "$0")/.."

PY=.venv/bin/python
APP=dist/MooMooTrader.app
DMG=dist/MooMooTrader.dmg
ICNS=packaging/MooMooTrader.icns

# ── icon: build .icns from the largest raster we have ──────────────────────────
# (swap web/static/apple-touch-icon.png for a 1024×1024 master when you have one)
if [ ! -f "$ICNS" ] || [ web/static/apple-touch-icon.png -nt "$ICNS" ]; then
    echo "▸ building $ICNS"
    ICONSET=$(mktemp -d)/MooMooTrader.iconset
    mkdir -p "$ICONSET"
    SRC=web/static/apple-touch-icon.png
    for s in 16 32 64 128 256 512 1024; do
        sips -z $s $s "$SRC" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
    done
    mv "$ICONSET/icon_1024x1024.png" "$ICONSET/icon_512x512@2x.png"
    cp "$ICONSET/icon_512x512.png"  "$ICONSET/icon_256x256@2x.png"
    cp "$ICONSET/icon_256x256.png"  "$ICONSET/icon_128x128@2x.png"
    cp "$ICONSET/icon_64x64.png"    "$ICONSET/icon_32x32@2x.png"
    rm  "$ICONSET/icon_64x64.png"
    cp "$ICONSET/icon_32x32.png"    "$ICONSET/icon_16x16@2x.png"
    iconutil -c icns "$ICONSET" -o "$ICNS"
fi

# ── build ──────────────────────────────────────────────────────────────────────
echo "▸ PyInstaller build (this takes a few minutes)…"
"$PY" -m PyInstaller --noconfirm --clean packaging/MooMooTrader.spec \
      --distpath dist --workpath build

# ── sign ───────────────────────────────────────────────────────────────────────
if [ -n "${CODESIGN_ID:-}" ]; then
    echo "▸ codesign (Developer ID: $CODESIGN_ID)"
    codesign --force --deep --options runtime --timestamp \
             --sign "$CODESIGN_ID" "$APP"
else
    echo "▸ codesign (ad-hoc — fine for local testing, NOT for distribution)"
    codesign --force --deep --sign - "$APP"
fi

# ── DMG: drag-to-Applications ──────────────────────────────────────────────────
echo "▸ building $DMG"
STAGE=$(mktemp -d)
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
rm -f "$DMG"
hdiutil create -volname "MooMoo Trader" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null
rm -rf "$STAGE"

echo
echo "✓ $APP"
echo "✓ $DMG  ($(du -h "$DMG" | cut -f1))"
[ -z "${CODESIGN_ID:-}" ] && echo "⚠ ad-hoc signature — for sale builds set CODESIGN_ID and notarize (see header)."
