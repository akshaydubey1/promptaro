#!/bin/bash
# Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.
# Adds Tillwright to the Linux app menu — Author: Akshay Dubey
set -e
cd "$(dirname "$0")/.."
SRC="$(pwd)"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICONS="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/256x256/apps"
mkdir -p "$APPS" "$ICONS"
cp packaging/icon-256.png "$ICONS/tillwright.png"
cat > "$APPS/tillwright.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=Tillwright
Comment=Every AI model, the right one for each task
Exec="$SRC/launch.sh" --background
Icon=tillwright
Terminal=false
Categories=Development;Utility;
DESK
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" || true
echo "Tillwright is now in your app menu."
