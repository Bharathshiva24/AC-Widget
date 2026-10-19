#!/bin/bash
# Installs (or updates) the AC app in Applications and opens it. Your saved login is not touched.
DIR="$HOME/ac-control"
SRC="$DIR/AC.app"
chmod +x "$SRC/Contents/MacOS/ac"
/usr/bin/pkill -f "ac_panel.py" 2>/dev/null; sleep 1
rm -rf "/Applications/AC Panel.app" "$DIR/AC Panel.app" "/Applications/MY AC.app" "$DIR/MY AC.app" 2>/dev/null
APP="$SRC"
if rm -rf "/Applications/AC.app" 2>/dev/null && cp -R "$SRC" /Applications/ 2>/dev/null; then APP="/Applications/AC.app"; fi
/usr/bin/xattr -dr com.apple.quarantine "$APP" 2>/dev/null
open "$APP" && echo "Done. AC is installed at: $APP"
echo "Look for AC in your menubar. You can close this Terminal window."
