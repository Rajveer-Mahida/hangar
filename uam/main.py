"""Entry point."""

from __future__ import annotations

import logging
import os
import sys


def main(argv: list[str] | None = None) -> int:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    gi.require_version("Gdk", "4.0")
    gi.require_version("Graphene", "1.0")
    gi.require_version("Gsk", "4.0")

    logging.basicConfig(level=os.environ.get("UAM_LOG_LEVEL", "WARNING"),
                        format="%(levelname)s %(name)s: %(message)s")
    if os.geteuid() == 0:
        from . import APP_NAME

        print(f"{APP_NAME} must not be run as root. Administrative actions use PolicyKit when needed.",
              file=sys.stderr)
        return 1
    from .application import Application

    return Application().run(argv if argv is not None else sys.argv)


if __name__ == "__main__":
    sys.exit(main())
