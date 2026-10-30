"""Preferences."""

from __future__ import annotations

import os
import time

from gi.repository import Adw, GLib, Gtk

from ..core.safety import FORBIDDEN_TREES
from ..core.settings import PROVIDER_IDS
from ..services.inventory_service import AppController
from .widgets import esc

THEMES = [("system", "Follow system"), ("light", "Light"), ("dark", "Dark")]
PROVIDER_NAMES = {"apt": "APT / dpkg", "snap": "Snap", "flatpak": "Flatpak", "appimage": "AppImage scanner",
                  "manual": "Manual software scanner", "github": "GitHub release checks"}
PROVIDER_HINTS = {"github": "Asks api.github.com for new releases of apps installed from GitHub (AppImages, "
                            "downloaded .deb files). GitHub sees which repositories are checked."}


def apply_theme(theme: str) -> None:
    manager = Adw.StyleManager.get_default()
    manager.set_color_scheme({
        "light": Adw.ColorScheme.FORCE_LIGHT,
        "dark": Adw.ColorScheme.FORCE_DARK,
    }.get(theme, Adw.ColorScheme.DEFAULT))


class SettingsPage(Gtk.Box):
    __gtype_name__ = "UamSettingsPage"

    def __init__(self, controller: AppController, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.controller = controller
        self.window = window
        self.settings = controller.settings
        self.page = Adw.PreferencesPage()
        self.append(self.page)
        self._build()

    def _set(self, name: str, value, rescan: bool = False, label: str = "") -> None:
        if getattr(self.settings, name) == value:
            return
        self.settings.set(name, value)
        self.controller.log_action("settings", label or name.replace("_", " "), "success", detail=str(value))
        if rescan:
            self.controller.rescan("settings")

    def _build(self) -> None:
        s = self.settings

        # Appearance ----------------------------------------------------------------
        group = Adw.PreferencesGroup(title="Appearance")
        theme = Adw.ComboRow(title="Style", model=Gtk.StringList.new([label for _, label in THEMES]))
        theme.set_selected([k for k, _ in THEMES].index(s.theme) if s.theme in dict(THEMES) else 0)

        def on_theme(row, _p):
            value = THEMES[row.get_selected()][0]
            apply_theme(value)
            self._set("theme", value, label="theme")

        theme.connect("notify::selected", on_theme)
        group.add(theme)
        self.page.add(group)

        # Inventory ---------------------------------------------------------------------
        group = Adw.PreferencesGroup(title="Inventory")
        auto = Adw.SwitchRow(title="Automatic inventory refresh", subtitle="Rescan in the background periodically",
                             active=s.auto_refresh)
        interval = Adw.SpinRow.new_with_range(5, 1440, 5)
        interval.set_title("Scan interval (minutes)")
        interval.set_value(s.scan_interval_minutes)
        interval.set_sensitive(s.auto_refresh)

        def on_auto(row, _p):
            self._set("auto_refresh", row.get_active())
            interval.set_sensitive(row.get_active())
            self.controller.reschedule()

        def on_interval(row, _p):
            self._set("scan_interval_minutes", int(row.get_value()), label="scan interval")
            self.controller.reschedule()

        auto.connect("notify::active", on_auto)
        interval.connect("notify::value", on_interval)
        group.add(auto)
        group.add(interval)
        watch = Adw.SwitchRow(title="Watch launcher folders",
                              subtitle="Rescan automatically when launchers or AppImages appear or disappear",
                              active=s.watch_launchers)

        def on_watch(row, _p):
            self._set("watch_launchers", row.get_active())
            self.controller.setup_monitors()

        watch.connect("notify::active", on_watch)
        group.add(watch)
        for name, title, subtitle in (
            ("include_hidden", "Include hidden applications",
             "Show launchers marked NoDisplay, such as URL handlers and helpers"),
            ("show_components", "Show system components",
             "Show runtimes, base snaps and packages essential to the system"),
            ("group_duplicates", "Group multiple installations",
             "Show one entry when an app is installed through several sources"),
        ):
            row = Adw.SwitchRow(title=title, subtitle=subtitle, active=getattr(s, name))
            row.connect("notify::active", lambda r, _p, n=name: (self._set(n, r.get_active()),
                                                                  self.controller.emit("inventory-changed")))
            group.add(row)
        self.page.add(group)

        # Folders ------------------------------------------------------------------------
        self.app_dirs_group = Adw.PreferencesGroup(
            title="AppImage folders",
            description="Folders searched for AppImages (one level deep). ~/Applications, ~/Downloads and "
                        "/opt are always included.")
        self._fill_dirs(self.app_dirs_group, "application_dirs")
        self.page.add(self.app_dirs_group)
        self.custom_dirs_group = Adw.PreferencesGroup(
            title="Custom application folders",
            description="Each sub-folder of these folders is treated as a manually installed application "
                        "(like /opt). Files are never modified by scanning.")
        self._fill_dirs(self.custom_dirs_group, "custom_dirs")
        self.page.add(self.custom_dirs_group)

        # Package managers ----------------------------------------------------------------
        group = Adw.PreferencesGroup(title="Integrations")
        for pid in PROVIDER_IDS:
            row = Adw.SwitchRow(title=PROVIDER_NAMES[pid], active=s.provider_enabled(pid))
            if pid in PROVIDER_HINTS:
                row.set_subtitle(PROVIDER_HINTS[pid])

            def on_provider(r, _p, p=pid):
                if r.get_active() != self.settings.provider_enabled(p):
                    self.settings.set_provider(p, r.get_active())
                    self.controller.log_action("settings", f"{p} integration", "success",
                                               detail="enabled" if r.get_active() else "disabled")
                    self.controller.rescan("settings")

            row.connect("notify::active", on_provider)
            group.add(row)
        self.page.add(group)

        # Updates & safety ------------------------------------------------------------------
        group = Adw.PreferencesGroup(title="Updates")
        row = Adw.SwitchRow(title="Check for updates automatically",
                            subtitle="APT after every scan; Snap and Flatpak at the interval below",
                            active=s.auto_check_updates)
        row.connect("notify::active", lambda r, _p: self._set("auto_check_updates", r.get_active()))
        group.add(row)
        spin = Adw.SpinRow.new_with_range(1, 168, 1)
        spin.set_title("Store check interval (hours)")
        spin.set_value(s.update_check_interval_hours)
        spin.connect("notify::value", lambda r, _p: self._set("update_check_interval_hours", int(r.get_value()),
                                                              label="update interval"))
        group.add(spin)
        self.page.add(group)

        group = Adw.PreferencesGroup(title="Safety")
        row = Adw.SwitchRow(
            title="Confirm before uninstalling",
            subtitle="When off, only reversible removals (moving your own files to the Trash) skip the "
                     "confirmation. Package removals and system files always show a preview and require "
                     "authentication.",
            active=s.confirm_uninstall)
        row.connect("notify::active", lambda r, _p: self._set("confirm_uninstall", r.get_active()))
        group.add(row)
        self.page.add(group)

        # Data ---------------------------------------------------------------------------------
        group = Adw.PreferencesGroup(title="Data")
        row = Adw.ActionRow(title="Inventory database", subtitle=esc(self.controller.db.path),
                            subtitle_selectable=True)
        row.add_css_class("property")
        group.add(row)
        row = Adw.ActionRow(title="Back up inventory database",
                            subtitle="Save a copy including reviews, history and settings")
        button = Gtk.Button(label="Back Up…", valign=Gtk.Align.CENTER)
        button.connect("clicked", self._on_backup)
        row.add_suffix(button)
        group.add(row)
        row = Adw.ActionRow(title="Forget review decisions",
                            subtitle="Mark all manually reviewed items as unreviewed again")
        button = Gtk.Button(label="Reset…", valign=Gtk.Align.CENTER)
        button.add_css_class("destructive-action")
        button.connect("clicked", self._on_reset_reviews)
        row.add_suffix(button)
        group.add(row)
        self.page.add(group)

    # -- folders ------------------------------------------------------------------------------
    def _fill_dirs(self, group: Adw.PreferencesGroup, name: str) -> None:
        for row in getattr(group, "_rows", []):
            group.remove(row)
        group._rows = []
        for path in getattr(self.settings, name):
            row = Adw.ActionRow(title=esc(path))
            if not os.path.isdir(os.path.expanduser(path)):
                row.set_subtitle("Folder not found")
            remove = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Remove")
            remove.add_css_class("flat")
            remove.connect("clicked", lambda _b, p=path: self._remove_dir(group, name, p))
            row.add_suffix(remove)
            group.add(row)
            group._rows.append(row)
        add_row = Adw.ActionRow(title="Add Folder…", activatable=True)
        add_row.add_prefix(Gtk.Image.new_from_icon_name("list-add-symbolic"))
        add_row.connect("activated", lambda *_: self._add_dir(group, name))
        group.add(add_row)
        group._rows.append(add_row)

    def _add_dir(self, group, name: str) -> None:
        dialog = Gtk.FileDialog(title="Choose a Folder to Scan", modal=True)

        def done(d, result):
            try:
                folder = d.select_folder_finish(result)
            except GLib.Error:
                return
            path = folder.get_path() if folder else None
            if not path:
                return
            path = os.path.realpath(path)
            home = os.path.realpath(os.path.expanduser("~"))
            too_broad = path in ("/", "/home", "/usr", "/var", "/etc", home) or any(
                path == t or path.startswith(t + "/") for t in FORBIDDEN_TREES)
            if too_broad:
                self.window.toast("That folder is too broad or is a protected system folder")
                return
            current = list(getattr(self.settings, name))
            if path not in current:
                current.append(path)
                self._set(name, current, rescan=True, label=f"added folder {path}")
                self._fill_dirs(group, name)
                self.controller.setup_monitors()

        dialog.select_folder(self.window, None, done)

    def _remove_dir(self, group, name: str, path: str) -> None:
        current = [p for p in getattr(self.settings, name) if p != path]
        self._set(name, current, rescan=True, label=f"removed folder {path}")
        self._fill_dirs(group, name)
        self.controller.setup_monitors()

    # -- data ------------------------------------------------------------------------------------
    def _on_backup(self, _button) -> None:
        dialog = Gtk.FileDialog(title="Back Up Inventory Database", modal=True,
                                initial_name=time.strftime("hangar-%Y%m%d-%H%M.db"))

        def done(d, result):
            try:
                file = d.save_finish(result)
            except GLib.Error:
                return
            path = file.get_path() if file else None
            if not path:
                return
            try:
                self.controller.db.backup(path)
            except (OSError, ValueError, Exception) as exc:
                self.controller.log_action("backup", "inventory database", "failed", detail=str(exc))
                self.window.toast(f"Backup failed: {exc}")
                return
            self.controller.log_action("backup", "inventory database", "success", detail=path)
            self.window.toast("Database backed up")

        dialog.save(self.window, None, done)

    def _on_reset_reviews(self, _button) -> None:
        dialog = Adw.AlertDialog(heading="Forget Review Decisions?",
                                 body="All items you marked as application, component or ignored become "
                                      "unreviewed again.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("reset", "Reset")
        dialog.set_response_appearance("reset", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")

        def on_response(_d, response):
            if response != "reset":
                return
            inv = self.controller.inventory
            for record in inv.records.values():
                if record.classification is not None:
                    inv.classify(record, None)
            for key in list(self.controller.db.classifications()):
                self.controller.db.set_classification(key, None)
            self.controller.log_action("settings", "review decisions", "success", detail="reset")
            self.controller.emit("inventory-changed")

        dialog.connect("response", on_response)
        dialog.present(self.window)

