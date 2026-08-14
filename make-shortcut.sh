#!/bin/bash
# Build CalSync.app — a double-clickable launcher for the review page.
#
# The app holds no logic of its own: it makes sure the background agent is
# loaded and then hands off to `calsync open`, which reads the current port and
# token from the config. That way the shortcut keeps working if either changes.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
DEST="${1:-$HOME/Desktop}"
APP="$DEST/CalSync.app"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>CalSync</string>
    <key>CFBundleDisplayName</key><string>CalSync</string>
    <key>CFBundleIdentifier</key><string>local.calsync.launcher</string>
    <key>CFBundleExecutable</key><string>CalSync</string>
    <key>CFBundleIconFile</key><string>CalSync</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>CFBundleVersion</key><string>1</string>
    <key>LSUIElement</key><true/>
</dict>
PLIST
echo "</plist>" >> "$APP/Contents/Info.plist"

cat > "$APP/Contents/MacOS/CalSync" <<EOF
#!/bin/bash
CALSYNC="$ROOT/bin/calsync"
# Reload the agent if it is not running, so the page is never a dead link.
if ! launchctl print "gui/\$(id -u)/local.calsync.agent" >/dev/null 2>&1; then
    "\$CALSYNC" install >/dev/null 2>&1 || true
    sleep 1
fi
exec "\$CALSYNC" open
EOF
chmod +x "$APP/Contents/MacOS/CalSync"

echo "Drawing the icon..."
WORK="$(mktemp -d)"
python3 "$ROOT/src/make_icon.py" "$WORK/icon.png"

ICONSET="$WORK/CalSync.iconset"
mkdir -p "$ICONSET"
for size in 16 32 128 256 512; do
    sips -z $size $size "$WORK/icon.png" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
    sips -z $((size * 2)) $((size * 2)) "$WORK/icon.png" \
        --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/CalSync.icns"
rm -rf "$WORK"

codesign --force --sign - --identifier local.calsync.launcher "$APP" 2>/dev/null || true

# Nudge Finder and LaunchServices so the new icon shows up straight away.
touch "$APP"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
    -f "$APP" 2>/dev/null || true

echo "Built $APP"
