#!/usr/bin/env bash
# Run Hangar from the source tree (no installation needed).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! python3 - <<'PY' 2>/dev/null
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw  # noqa: F401
PY
then
    echo "GTK 4 / libadwaita Python bindings are missing. Run: ./scripts/install-deps.sh" >&2
    exit 1
fi

case "${1:-}" in
    --test|test)
        shift || true
        cd "$HERE"
        exec python3 -m pytest -q tests "$@"
        ;;
esac

# Development mode: administrative actions run the in-tree helper through pkexec.
export HANGAR_DEV_HELPER=1
export PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -P -m hangar "$@"
