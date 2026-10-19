#!/bin/bash
# One-time setup for the AC app: saves your MirAIe login in the macOS Keychain and installs the app.
DIR="$HOME/ac-control"
echo "AC setup"
read -r -p "MirAIe mobile number (with +91): " MOBILE
if [ "$1" = "--show" ]; then
  read -r -p "MirAIe password (visible while typing): " PASS
else
  read -r -s -p "MirAIe password (hidden while typing): " PASS; echo
fi
[ -n "$MOBILE" ] && [ -n "$PASS" ] || { echo "Both are needed. Nothing was changed."; exit 1; }
/usr/bin/security add-generic-password -U -s ac-panel -a mobile -w "$MOBILE" || exit 1
/usr/bin/security add-generic-password -U -s ac-panel -a password -w "$PASS" || exit 1
echo "Login saved in your Keychain."
bash "$DIR/install.sh"
