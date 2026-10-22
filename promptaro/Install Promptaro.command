#!/bin/bash
# Copyright (c) 2026 Akshay Dubey. Licensed under the PolyForm Small Business License 1.0.0; see LICENSE.
# Installs Promptaro.app into your Applications folder and opens it — Author: Akshay Dubey
cd "$(dirname "$0")" && ./packaging/make-mac-app.sh || { read -n1 -p "Install failed. Press any key to close."; exit 1; }
APP="/Applications/Promptaro.app"; [ -d "$APP" ] || APP="$HOME/Applications/Promptaro.app"
xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true     # harmless when not quarantined
open "$APP"
read -n1 -p "Promptaro is installed and opening in your browser. Press any key to close." || true
