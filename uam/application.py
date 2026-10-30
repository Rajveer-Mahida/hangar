"""Adw.Application subclass: startup, styling, lifetime."""

from __future__ import annotations

import os

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from . import APP_ID, APP_NAME, HOMEPAGE, VERSION
from .services.inventory_service import AppController
from .ui.settings_page import apply_theme
from .ui.window import MainWindow

STYLE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources", "styles")


class Application(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        GLib.set_application_name(APP_NAME)
        self.controller: AppController | None = None
        self.window: MainWindow | None = None
        self._dark_provider: Gtk.CssProvider | None = None
        about = Gio.SimpleAction.new("about", None)
        about.connect("activate", self._on_about)
        self.add_action(about)
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Control>q"])

    def do_startup(self):
        Adw.Application.do_startup(self)
        self._load_css()
        # Running from the source tree: make the bundled app icon available.
        icons = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "icons")
        if os.path.isdir(icons):
            Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(icons)
        Gtk.Window.set_default_icon_name(APP_ID)
        self.controller = AppController()
        apply_theme(self.controller.settings.theme)

    def _load_css(self) -> None:
        display = Gdk.Display.get_default()
        provider = Gtk.CssProvider()
        provider.load_from_path(os.path.join(STYLE_DIR, "style.css"))
        Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self._dark_provider = Gtk.CssProvider()
        self._dark_provider.load_from_path(os.path.join(STYLE_DIR, "style-dark.css"))
        manager = Adw.StyleManager.get_default()
        manager.connect("notify::dark", lambda *_: self._sync_dark())
        self._sync_dark()

    def _sync_dark(self) -> None:
        display = Gdk.Display.get_default()
        if Adw.StyleManager.get_default().get_dark():
            Gtk.StyleContext.add_provider_for_display(display, self._dark_provider,
                                                      Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
        else:
            Gtk.StyleContext.remove_provider_for_display(display, self._dark_provider)

    def do_activate(self):
        if self.window is None:
            self.window = MainWindow(self, self.controller)
            self.controller.start()
        self.window.present()

    def do_open(self, files, _n_files, _hint):
        self.do_activate()
        paths = [f.get_path() for f in files if f.get_path()]
        if paths:
            GLib.idle_add(lambda: self.window.open_files(paths) or False)

    def do_shutdown(self):
        if self.controller is not None:
            self.controller.shutdown()
            self.controller.db.close()
        Adw.Application.do_shutdown(self)

    def _on_about(self, *_args) -> None:
        about = Adw.AboutDialog(
            application_name=APP_NAME,
            application_icon=APP_ID,
            version=VERSION,
            developer_name="Hangar contributors",
            comments="One inventory for every application on your computer: APT, Snap, Flatpak, AppImages, "
                     "GitHub downloads and manually installed software, kept up to date and removed cleanly.",
            license_type=Gtk.License.GPL_3_0,
            website=HOMEPAGE,
            issue_url=f"{HOMEPAGE}/issues",
        )
        about.present(self.window)
