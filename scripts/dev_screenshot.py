#!/usr/bin/env python3
"""Development helper: start the app, navigate, render the window to PNG, quit.

Usage: scripts/dev_screenshot.py OUTPUT.png [section] [--dark] [--width W --height H] [--details KEY-SUBSTRING]
Uses a temporary database unless UAM_DB is set. Never performs package operations.
"""
import argparse
import faulthandler
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import uam.ui  # noqa: F401  (sets GI versions)
from gi.repository import Adw, Gio, GLib, Graphene, Gtk  # noqa: E402

from uam.application import Application  # noqa: E402
from uam.core.database import Database  # noqa: E402
from uam.services.inventory_service import AppController  # noqa: E402


def capture(widget, path):
    paintable = Gtk.WidgetPaintable.new(widget)
    w, h = widget.get_width(), widget.get_height()
    snap = Gtk.Snapshot.new()
    paintable.snapshot(snap, w, h)
    node = snap.to_node()
    texture = widget.get_native().get_renderer().render_texture(node, Graphene.Rect().init(0, 0, w, h))
    texture.save_to_png(path)


def main():
    if os.environ.get("UAM_SHOT_TIMEOUT"):
        faulthandler.dump_traceback_later(float(os.environ["UAM_SHOT_TIMEOUT"]), exit=True)
    ap = argparse.ArgumentParser()
    ap.add_argument("output")
    ap.add_argument("section", nargs="?", default="overview")
    ap.add_argument("--dark", action="store_true")
    ap.add_argument("--light", action="store_true")
    ap.add_argument("--grid", action="store_true")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=820)
    ap.add_argument("--details", default="")
    ap.add_argument("--dialog", default="")
    ap.add_argument("--install", default="")
    ap.add_argument("--bulk", default="", help="comma-separated name fragments for the bulk uninstall dialog")
    ap.add_argument("--op-demo", action="store_true", help="show the operation bar with a harmless demo step")
    ap.add_argument("--delay", type=float, default=2.5)
    args = ap.parse_args()

    db_path = os.environ.get("UAM_DB") or os.path.join(os.path.dirname(os.path.abspath(args.output)), "shot.db")

    class ShotApp(Application):
        def __init__(self):
            super().__init__()
            # Do not hand over to an already running instance of the app.
            self.set_flags(self.get_flags() | Gio.ApplicationFlags.NON_UNIQUE)

        def do_startup(self):
            Adw.Application.do_startup(self)
            self._load_css()
            self.controller = AppController(Database(db_path))
            if args.dark:
                Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
            if args.light:
                Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)

        def do_activate(self):
            Application.do_activate(self)
            self.window.set_default_size(args.width, args.height)
            done = {"scan": False}

            def on_scan(_c, running, _m, _f):
                if not running and not done["scan"]:
                    done["scan"] = True
                    GLib.timeout_add(300, navigate)

            def navigate():
                win = self.window
                win.show_section(args.section)
                if args.grid:
                    win.applications.grid_button.set_active(True)
                if args.details:
                    for key, rec in self.controller.inventory.records.items():
                        if args.details.lower() in (rec.name + key).lower():
                            win.show_app(key)
                            break
                if args.dialog:
                    rec = next((r for k, r in self.controller.inventory.records.items()
                                if args.dialog.lower() in (r.name + k).lower()), None)
                    if rec:
                        win.uninstall([rec])
                if args.op_demo:
                    import time
                    from uam.services.operation_service import Operation, PythonStep

                    def demo(log):
                        for i in range(30):
                            log(f"demo line {i}")
                            time.sleep(0.2)

                    self.controller.submit(Operation("Updating Demo App", "update", ["Demo App"],
                                                     [PythonStep(demo, "Downloading demo data")], source="Demo"))
                    win.operation_bar.details.set_active(True)
                if args.bulk:
                    wanted = [w.strip().lower() for w in args.bulk.split(",")]
                    recs = [r for r in self.controller.inventory.records.values()
                            if any(w == r.name.lower() for w in wanted)]
                    win.uninstall(recs)
                if args.install:
                    from uam.ui.install_dialog import InstallDialog
                    dlg = InstallDialog(win)
                    dlg.present(win)
                    dlg.entry.set_text(args.install)
                GLib.timeout_add(int(args.delay * 1000), shoot)
                return False

            def shoot():
                target = self.window
                capture(target, args.output)
                print("saved", args.output)
                self.quit()
                return False

            self.controller.connect("scan-state", on_scan)

    return ShotApp().run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(main())
