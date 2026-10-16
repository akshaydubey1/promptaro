#!/bin/bash
# Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.
# Builds Tillwright.app (Applications folder, Dock, Spotlight, Launchpad) — Author: Akshay Dubey
# The app is a small wrapper that starts Tillwright from this folder, so updates
# to this folder are picked up without reinstalling.
set -e
cd "$(dirname "$0")/.."
SRC="$(pwd)"
DEST="${1:-/Applications}"
[ -w "$DEST" ] || DEST="$HOME/Applications"
mkdir -p "$DEST"
APP="$DEST/Tillwright.app"
VERSION="$(python3 -c 'import json;print(json.load(open("manifest.json")).get("version","1.0.0"))' 2>/dev/null || echo 1.0.0)"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Tillwright</string>
  <key>CFBundleDisplayName</key><string>Tillwright</string>
  <key>CFBundleIdentifier</key><string>com.akshaydubey.tillwright</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>Tillwright</string>
  <key>CFBundleIconFile</key><string>Tillwright</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHumanReadableCopyright</key><string>Copyright © 2026 Akshay Dubey. All rights reserved.</string>
</dict></plist>
PLIST

cat > "$APP/Contents/MacOS/Tillwright" <<RUN
#!/bin/bash
# Tillwright — Author: Akshay Dubey
exec "$SRC/launch.sh" --background
RUN
chmod +x "$APP/Contents/MacOS/Tillwright"

# Icon: build a .icns from the PNG with macOS's own tools.
ICONSET="$(mktemp -d)/Tillwright.iconset"
mkdir -p "$ICONSET"
for s in 16 32 128 256 512; do
  sips -z $s $s packaging/icon.png --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  sips -z $((s*2)) $((s*2)) packaging/icon.png --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/Tillwright.icns"
touch "$APP"
echo "Installed $APP"
echo "Open it from Applications, Launchpad or Spotlight. Drag it to the Dock to keep it there."
open -R "$APP" 2>/dev/null || true
