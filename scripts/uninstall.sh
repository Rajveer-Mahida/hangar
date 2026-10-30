#!/usr/bin/env bash
# Remove a system-wide installation made by scripts/install.sh: sudo ./scripts/uninstall.sh [PREFIX]
# Your inventory database (~/.local/share/hangar) is kept.
set -euo pipefail
if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo: sudo $0 ${1:-/usr/local}" >&2
    exit 1
fi
PREFIX="${1:-/usr/local}"
APP_IDS=("io.github.Rajveer_Mahida.Hangar" "io.github.uam.UnifiedAppManager")  # current, pre-1.0
rm -rf "$PREFIX/share/hangar" "$PREFIX/libexec/hangar" \
       "$PREFIX/share/unified-app-manager" "$PREFIX/libexec/unified-app-manager"
rm -f "$PREFIX/bin/hangar" "$PREFIX/bin/unified-app-manager"
for APP_ID in "${APP_IDS[@]}"; do
    rm -f "$PREFIX/share/applications/$APP_ID.desktop" \
          "$PREFIX/share/icons/hicolor/scalable/apps/$APP_ID.svg" \
          "$PREFIX/share/icons/hicolor/symbolic/apps/$APP_ID-symbolic.svg" \
          "$PREFIX/share/metainfo/$APP_ID.metainfo.xml" \
          "/usr/share/polkit-1/actions/$APP_ID.policy"
done
gtk-update-icon-cache -q -t -f "$PREFIX/share/icons/hicolor" 2>/dev/null || true
update-desktop-database -q "$PREFIX/share/applications" 2>/dev/null || true
echo "Removed Hangar from $PREFIX"
