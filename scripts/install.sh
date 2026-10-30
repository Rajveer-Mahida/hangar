#!/usr/bin/env bash
# System-wide installation of Hangar (needs root): sudo ./scripts/install.sh [PREFIX]
# Installs the app under PREFIX (default /usr/local), the root-owned privileged
# helper under PREFIX/libexec and its PolicyKit action in /usr/share/polkit-1.
set -euo pipefail
if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo: sudo $0 ${1:-/usr/local}" >&2
    exit 1
fi
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX="${1:-/usr/local}"
APPDIR="$PREFIX/share/hangar"
HELPER_DIR="$PREFIX/libexec/hangar"
HELPER="$HELPER_DIR/hangar-helper"
APP_ID="io.github.Rajveer_Mahida.Hangar"
LEGACY_ID="io.github.uam.UnifiedAppManager"

# The PolicyKit actions point at the helper; every directory above it must be
# root-owned and not writable by other users, otherwise anyone could replace it.
case "$PREFIX" in
    /*) ;;
    *) echo "PREFIX must be an absolute path" >&2; exit 1 ;;
esac
check="$PREFIX"
while :; do
    if [[ -e "$check" ]]; then
        read -r owner mode < <(stat -c '%u %a' "$check")
        if [[ "$owner" != 0 ]] || (( 8#$mode & 022 )); then
            echo "Refusing to install: $check is not root-owned or is writable by other users." >&2
            exit 1
        fi
    fi
    [[ "$check" == "/" ]] && break
    check="$(dirname "$check")"
done

install -d -m 0755 "$APPDIR" "$HELPER_DIR" "$PREFIX/bin" "$PREFIX/share/applications" \
    "$PREFIX/share/icons/hicolor/scalable/apps" "$PREFIX/share/icons/hicolor/symbolic/apps" \
    "$PREFIX/share/metainfo" /usr/share/polkit-1/actions

rm -rf "$APPDIR/uam"
cp -r "$SRC/uam" "$APPDIR/uam"
find "$APPDIR/uam" -name '__pycache__' -prune -exec rm -rf {} +
chown -R root:root "$APPDIR"
chmod -R u=rwX,go=rX "$APPDIR"

install -m 0755 -o root -g root "$SRC/uam/helper/uam_helper.py" "$HELPER"
install -m 0755 "$SRC/bin/hangar" "$PREFIX/bin/hangar"

# Pre-1.0 builds were called "Unified App Manager" and used another app ID; remove them.
rm -rf "$PREFIX/share/unified-app-manager" "$PREFIX/libexec/unified-app-manager"
rm -f "$PREFIX/bin/unified-app-manager" "$PREFIX/share/applications/$LEGACY_ID.desktop" \
      "$PREFIX/share/icons/hicolor/scalable/apps/$LEGACY_ID.svg" \
      "$PREFIX/share/icons/hicolor/symbolic/apps/$LEGACY_ID-symbolic.svg" \
      "$PREFIX/share/metainfo/$LEGACY_ID.metainfo.xml" "/usr/share/polkit-1/actions/$LEGACY_ID.policy"
install -m 0644 "$SRC/data/$APP_ID.desktop" "$PREFIX/share/applications/$APP_ID.desktop"
install -m 0644 "$SRC/data/icons/hicolor/scalable/apps/$APP_ID.svg" "$PREFIX/share/icons/hicolor/scalable/apps/"
install -m 0644 "$SRC/data/icons/hicolor/symbolic/apps/$APP_ID-symbolic.svg" "$PREFIX/share/icons/hicolor/symbolic/apps/"
install -m 0644 "$SRC/data/$APP_ID.metainfo.xml" "$PREFIX/share/metainfo/"
sed "s|@HELPER_PATH@|$HELPER|" "$SRC/data/$APP_ID.policy.in" > "/usr/share/polkit-1/actions/$APP_ID.policy"
chmod 0644 "/usr/share/polkit-1/actions/$APP_ID.policy"

gtk-update-icon-cache -q -t -f "$PREFIX/share/icons/hicolor" 2>/dev/null || true
update-desktop-database -q "$PREFIX/share/applications" 2>/dev/null || true

echo "Installed to $PREFIX"
echo "  app:    $PREFIX/bin/hangar"
echo "  helper: $HELPER (root-owned, PolicyKit actions $APP_ID.<verb>)"
