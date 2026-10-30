#!/usr/bin/env bash
# Build a binary .deb in dist/ without root (dpkg-deb --root-owner-group).
# Install it with: sudo apt install ./dist/hangar_<version>_all.deb
set -euo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$(python3 -c "import sys; sys.path.insert(0, '$SRC'); import uam; print(uam.VERSION)")"
APP_ID="io.github.Rajveer_Mahida.Hangar"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
ROOT="$STAGE/root"
HELPER="/usr/libexec/hangar/hangar-helper"

install -d "$ROOT/DEBIAN" "$ROOT/usr/bin" "$ROOT/usr/share/hangar" "$ROOT/usr/libexec/hangar" \
    "$ROOT/usr/share/applications" "$ROOT/usr/share/icons/hicolor/scalable/apps" \
    "$ROOT/usr/share/icons/hicolor/symbolic/apps" "$ROOT/usr/share/metainfo" "$ROOT/usr/share/polkit-1/actions" \
    "$ROOT/usr/share/doc/hangar"

cp -r "$SRC/uam" "$ROOT/usr/share/hangar/uam"
find "$ROOT/usr/share/hangar" -name '__pycache__' -prune -exec rm -rf {} +
install -m 0755 "$SRC/uam/helper/uam_helper.py" "$ROOT$HELPER"
install -m 0755 "$SRC/bin/hangar" "$ROOT/usr/bin/hangar"
install -m 0644 "$SRC/data/$APP_ID.desktop" "$ROOT/usr/share/applications/"
install -m 0644 "$SRC/data/icons/hicolor/scalable/apps/$APP_ID.svg" "$ROOT/usr/share/icons/hicolor/scalable/apps/"
install -m 0644 "$SRC/data/icons/hicolor/symbolic/apps/$APP_ID-symbolic.svg" "$ROOT/usr/share/icons/hicolor/symbolic/apps/"
install -m 0644 "$SRC/data/$APP_ID.metainfo.xml" "$ROOT/usr/share/metainfo/"
sed "s|@HELPER_PATH@|$HELPER|" "$SRC/data/$APP_ID.policy.in" > "$ROOT/usr/share/polkit-1/actions/$APP_ID.policy"
install -m 0644 "$SRC/README.md" "$ROOT/usr/share/doc/hangar/README.md"
chmod -R u=rwX,go=rX "$ROOT/usr"
chmod 0755 "$ROOT$HELPER" "$ROOT/usr/bin/hangar"

SIZE_KB="$(du -sk "$ROOT/usr" | cut -f1)"
cat > "$ROOT/DEBIAN/control" <<CONTROL
Package: hangar
Version: $VERSION
Section: admin
Priority: optional
Architecture: all
Installed-Size: $SIZE_KB
Depends: python3 (>= 3.10), python3-gi (>= 3.42), gir1.2-gtk-4.0, gir1.2-adw-1 (>= 1.4), pkexec | policykit-1
Recommends: python3-apt, gir1.2-appstream-1.0, snapd, flatpak
Conflicts: unified-app-manager
Replaces: unified-app-manager
Maintainer: Rajveer Mahida <hello@rajveerx.in>
Homepage: https://github.com/Rajveer-Mahida/hangar
Description: one inventory for every installed application
 Detects applications installed through APT, Snap, Flatpak, AppImages,
 vendor packages and manual installs and shows them in one GTK4/libadwaita
 interface with update checks and safe, previewed uninstallation.
CONTROL

mkdir -p "$SRC/dist"
OUT="$SRC/dist/hangar_${VERSION}_all.deb"
dpkg-deb --root-owner-group -Zxz --build "$ROOT" "$OUT"
echo "Built $OUT"
