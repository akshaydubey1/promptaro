#!/bin/bash
# Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.
# Builds Promptaro.app (Applications folder, Dock, Spotlight, Launchpad) — Author: Akshay Dubey
# The app is a small wrapper that starts Promptaro from this folder, so updates
# to this folder are picked up without reinstalling.
set -e
cd "$(dirname "$0")/.."
SRC="$(pwd)"
DEST="${1:-/Applications}"
[ -w "$DEST" ] || DEST="$HOME/Applications"
mkdir -p "$DEST"
APP="$DEST/Promptaro.app"
rm -rf "$DEST/Tillwright.app" "$HOME/Applications/Tillwright.app" 2>/dev/null || true   # replace the old app
VERSION="$(python3 -c 'import json;print(json.load(open("manifest.json")).get("version","1.0.0"))' 2>/dev/null || echo 1.0.0)"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Promptaro</string>
  <key>CFBundleDisplayName</key><string>Promptaro</string>
  <key>CFBundleIdentifier</key><string>com.akshaydubey.promptaro</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>Promptaro</string>
  <key>CFBundleIconFile</key><string>Promptaro</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHumanReadableCopyright</key><string>Copyright © 2026 Akshay Dubey. All rights reserved.</string>
</dict></plist>
PLIST

cat > "$APP/Contents/MacOS/Promptaro" <<RUN
#!/bin/bash
# Promptaro — Author: Akshay Dubey
exec "$SRC/launch.sh" --background
RUN
chmod +x "$APP/Contents/MacOS/Promptaro"

# Icon: build a .icns from the PNG with macOS's own tools.
ICONSET="$(mktemp -d)/Promptaro.iconset"
mkdir -p "$ICONSET"
for s in 16 32 128 256 512; do
  sips -z $s $s packaging/icon.png --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  sips -z $((s*2)) $((s*2)) packaging/icon.png --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/Promptaro.icns"
touch "$APP"
echo "Installed $APP"
echo "Open it from Applications, Launchpad or Spotlight. Drag it to the Dock to keep it there."
echo "The app isn't Apple-signed: if macOS says it's damaged or can't be opened, run"
echo "  xattr -dr com.apple.quarantine \"$APP\""
echo "or right-click it in Applications and choose Open."
