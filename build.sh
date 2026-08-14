#!/bin/bash
# Compile calbridge and wrap it in a minimal .app bundle.
#
# The bundle exists for one reason: macOS grants Calendar (TCC) permission to a
# code identity. A loose binary run from a shell makes the *shell* the grantee,
# which is both too broad and too fragile. A signed bundle with its own usage
# string gets its own entry in System Settings > Privacy & Security > Calendars.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
APP="$ROOT/build/CalSyncBridge.app"
MACOS="$APP/Contents/MacOS"

rm -rf "$APP"
mkdir -p "$MACOS"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>CalSyncBridge</string>
    <key>CFBundleDisplayName</key>
    <string>CalSyncBridge</string>
    <key>CFBundleIdentifier</key>
    <string>local.calsync.bridge</string>
    <key>CFBundleExecutable</key>
    <string>calbridge</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundleVersion</key>
    <string>1</string>
    <key>LSMinimumSystemVersion</key>
    <string>13.0</string>
    <key>LSUIElement</key>
    <true/>
    <key>NSCalendarsUsageDescription</key>
    <string>CalSync reads your personal calendar and blocks the matching time on your work calendar.</string>
    <key>NSCalendarsFullAccessUsageDescription</key>
    <string>CalSync reads your personal calendar and blocks the matching time on your work calendar.</string>
</dict>
</plist>
PLIST

echo "Compiling calbridge..."
# -swift-version 5: EventKit's completion-handler APIs trip Swift 6 strict
# concurrency checking, and this tool is single-threaded by construction.
swiftc -swift-version 5 -O \
    -framework EventKit -framework Foundation \
    -o "$MACOS/calbridge" \
    "$ROOT/src/calbridge.swift"

echo "Signing bundle (ad-hoc)..."
# A stable ad-hoc signature keeps the TCC grant across runs. Rebuilding changes
# the cdhash, so macOS may ask for Calendar permission again after a rebuild.
codesign --force --sign - --identifier local.calsync.bridge "$APP"

echo "Built $APP"
