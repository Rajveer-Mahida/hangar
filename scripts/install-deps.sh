#!/usr/bin/env bash
# Install the runtime (and optional test) dependencies on Ubuntu 24.04.
set -euo pipefail

PACKAGES=(
    python3
    python3-gi
    gir1.2-gtk-4.0
    gir1.2-adw-1
    gir1.2-appstream-1.0   # software catalogue search for "Install Software"
    python3-apt            # repository origins and APT update details
    pkexec                 # PolicyKit authentication for administrative actions
    dpkg-dev               # dpkg-deb, used by scripts/build-deb.sh
)
OPTIONAL=(
    python3-pytest         # ./run.sh --test
    flatpak                # Flatpak integration (skip if you do not use Flatpak)
)

if [[ "${1:-}" == "--with-optional" ]]; then
    PACKAGES+=("${OPTIONAL[@]}")
fi

echo "The following packages will be installed with apt:"
printf '  %s\n' "${PACKAGES[@]}"
sudo apt-get update
sudo apt-get install -y --no-install-recommends "${PACKAGES[@]}"
echo
echo "Done. Start the app with ./run.sh"
echo "Snap support uses the snapd service that ships with Ubuntu; nothing else is required."
