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
# The deployment target must be pinned. Left to itself swiftc targets the
# toolchain's own newest macOS, which can be a version that does not exist yet
# (a 26.x SDK happily emits minos 28.0). LaunchServices then refuses to launch
# the bundle with -10825, kLSIncompatibleApplicationVersionErr, even though the
# binary runs fine when executed directly — which makes it look like a
# registration problem rather than a build one.
DEPLOYMENT_TARGET=13.0

# -swift-version 5: EventKit's completion-handler APIs trip Swift 6 strict
# concurrency checking, and this tool is single-threaded by construction.
compile() {
    swiftc -swift-version 5 -O \
        -target "$1-apple-macos$DEPLOYMENT_TARGET" \
        -framework EventKit -framework UserNotifications -framework AppKit \
        -framework Foundation \
        -o "$2" "$ROOT/src/calbridge.swift"
}

# Build universal when the SDK can manage it, so the same checkout works on
# Apple silicon and Intel; fall back to this machine's architecture alone.
TMP="$(mktemp -d)"
HOST_ARCH="$(uname -m)"
if compile arm64 "$TMP/calbridge.arm64" 2>/dev/null \
   && compile x86_64 "$TMP/calbridge.x86_64" 2>/dev/null; then
    lipo -create "$TMP/calbridge.arm64" "$TMP/calbridge.x86_64" -output "$MACOS/calbridge"
    echo "  universal (arm64 + x86_64), macOS $DEPLOYMENT_TARGET+"
else
    compile "$HOST_ARCH" "$MACOS/calbridge"
    echo "  $HOST_ARCH only, macOS $DEPLOYMENT_TARGET+"
fi
rm -rf "$TMP"

echo "Signing bundle (ad-hoc)..."
# A stable ad-hoc signature keeps the TCC grant across runs. Rebuilding changes
# the cdhash, so macOS may ask for Calendar permission again after a rebuild.
codesign --force --sign - --identifier local.calsync.bridge "$APP"

# Re-register with LaunchServices. Without this, `open -a` can fail with
# -10825 against the stale record of the previous build until the system
# notices the bundle changed.
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
    -f "$APP" 2>/dev/null || true

echo "Built $APP"
