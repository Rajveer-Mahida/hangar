"""Application details."""

from __future__ import annotations

import os
import threading

from gi.repository import Adw, GLib, GObject, Gtk

from ..core.application_model import AppRecord, Classification, Confidence, PackageManager
from ..services.inventory_service import AppController
from ..services.uninstall_service import directory_size
from .widgets import app_image, confidence_pill, esc, format_date, pill, size_text, source_badge


class DetailsPage(Adw.NavigationPage):
    __gtype_name__ = "HangarDetailsPage"
    __gsignals__ = {"show-app": (GObject.SignalFlags.RUN_FIRST, None, (str,))}

    def __init__(self, controller: AppController, record: AppRecord, window):
        super().__init__(title=record.name, tag=f"details:{record.key}")
        self.controller = controller
        self.record = record
        self.window = window
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        menu = Gtk.MenuButton(icon_name="view-more-symbolic", tooltip_text="More actions")
        from .applications_page import build_app_menu

        menu.set_menu_model(build_app_menu(record, controller.inventory.update_for(record)))
        header.pack_end(menu)
        toolbar.add_top_bar(header)
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24, margin_top=24, margin_bottom=32,
                               margin_start=18, margin_end=18)
        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        sw.set_child(Adw.Clamp(maximum_size=860, child=self.content))
        toolbar.set_content(sw)
        self.set_child(toolbar)
        self._build()

    # ------------------------------------------------------------------------
    def _build(self) -> None:
        r = self.record
        inv = self.controller.inventory
        update = inv.update_for(r)

        hero = Gtk.Box(spacing=18)
        hero.add_css_class("details-hero")
        hero.append(app_image(r, 96))
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, valign=Gtk.Align.CENTER, hexpand=True)
        name = Gtk.Label(label=r.name, xalign=0, wrap=True, selectable=True)
        name.add_css_class("details-name")
        info.append(name)
        if r.summary:
            summary = Gtk.Label(label=r.summary, xalign=0, wrap=True)
            summary.add_css_class("dim-label")
            info.append(summary)
        badges = Gtk.Box(spacing=6)
        badges.append(source_badge(r.source))
        badges.append(confidence_pill(r.confidence))
        if update:
            badges.append(pill(f"Update {update.new_version}", "update"))
        if r.classification:
            badges.append(pill(f"Marked as {r.classification.value}", "neutral"))
        if r.hidden:
            badges.append(pill("Hidden launcher", "neutral"))
        info.append(badges)
        hero.append(info)
        self.content.append(hero)

        actions = Gtk.Box(spacing=8)
        if r.has_launcher and (r.desktop_file or r.exec_path):
            open_btn = Gtk.Button(label="Open")
            open_btn.add_css_class("suggested-action")
            open_btn.add_css_class("pill")
            open_btn.set_action_name("win.app-launch")
            open_btn.set_action_target_value(GLib.Variant.new_string(r.key))
            actions.append(open_btn)
        if update:
            upd = Gtk.Button(label="Update")
            upd.add_css_class("pill")
            upd.set_action_name("win.app-update")
            upd.set_action_target_value(GLib.Variant.new_string(r.key))
            actions.append(upd)
        if r.install_path:
            folder = Gtk.Button()
            folder.set_child(Adw.ButtonContent(icon_name="folder-open-symbolic", label="Open Folder"))
            folder.add_css_class("pill")
            folder.set_action_name("win.app-locate")
            folder.set_action_target_value(GLib.Variant.new_string(r.key))
            actions.append(folder)
        uninstall = Gtk.Button(label="Uninstall…")
        uninstall.add_css_class("destructive-action")
        uninstall.add_css_class("pill")
        uninstall.set_action_name("win.app-uninstall")
        uninstall.set_action_target_value(GLib.Variant.new_string(r.key))
        actions.append(uninstall)
        self.content.append(actions)

        for note in r.notes:
            box = Gtk.Box(spacing=10)
            box.add_css_class("note-box")
            box.append(Gtk.Image.new_from_icon_name("dialog-information-symbolic"))
            box.append(Gtk.Label(label=note, xalign=0, wrap=True, hexpand=True))
            self.content.append(box)

        if r.description and r.description != r.summary:
            group = Adw.PreferencesGroup(title="Description")
            label = Gtk.Label(label=r.description[:3000], xalign=0, wrap=True, selectable=True)
            label.add_css_class("body")
            group.add(label)
            self.content.append(group)

        # Information -----------------------------------------------------------
        group = Adw.PreferencesGroup(title="Information")
        self._row(group, "Version", r.version or "Unknown")
        self._row(group, "Source", r.source.label)
        self._row(group, "Package manager", r.package_manager.label if r.package_manager != PackageManager.NONE
                  else "None (not package-managed)")
        if r.package_name:
            self._row(group, "Package", r.package_name, mono=True)
        if r.origin:
            self._row(group, "Origin", r.origin)
        installed = format_date(r.install_date, with_time=True)
        if not installed:
            installed = f"Unknown (first seen {format_date(r.first_seen)})" if r.first_seen else "Unknown"
        self._row(group, "Installed", installed)
        self.size_row = self._row(group, "Size", size_text(r.size) or ("Calculating…" if self._can_measure()
                                                                        else "Unknown"))
        self._row(group, "Category", r.category)
        self._row(group, "Detection", {
            Confidence.MANAGED: "Managed — known to a package manager",
            Confidence.DETECTED: "Detected — found by scanning with good evidence",
            Confidence.UNKNOWN: "Unknown — application-like files without enough information",
        }[r.confidence])
        for key, label in (("channel", "Channel"), ("revision", "Revision"), ("confinement", "Confinement"),
                           ("installation", "Installation"), ("branch", "Branch"), ("section", "Section"),
                           ("maintainer", "Maintainer"), ("update_info", "Update information")):
            value = r.extra.get(key)
            if value:
                self._row(group, label, str(value), mono=key == "update_info")
        if r.extra.get("auto_installed") is not None:
            self._row(group, "Installed as", "Dependency (automatically)" if r.extra["auto_installed"]
                      else "Explicitly by a user")
        if r.homepage:
            row = Adw.ActionRow(title="Homepage", subtitle=esc(r.homepage), activatable=True)
            row.add_suffix(Gtk.Image.new_from_icon_name("adw-external-link-symbolic"))
            row.connect("activated", lambda *_: Gtk.UriLauncher.new(r.homepage).launch(self.window, None, None))
            group.add(row)
        self.content.append(group)

        # Update ---------------------------------------------------------------------
        if r.extra.get("local_deb"):
            group = Adw.PreferencesGroup(title="Updates")
            row = Adw.ActionRow(title="No repository provides updates for this package",
                                subtitle="It was installed from a downloaded .deb file. New releases on GitHub are "
                                         "shown below when the repository is linked.")
            row.add_prefix(Gtk.Image.new_from_icon_name("dialog-information-symbolic"))
            group.add(row)
            self.content.append(group)
        elif r.package_manager != PackageManager.NONE:
            group = Adw.PreferencesGroup(title="Updates")
            if update:
                row = Adw.ActionRow(title=esc(f"Version {update.new_version} is available"),
                                    subtitle=esc(f"Currently {update.current_version or r.version}"
                                                 + (f" · {size_text(update.download_size)} download"
                                                    if update.download_size else "")))
                row.add_prefix(Gtk.Image.new_from_icon_name("software-update-available-symbolic"))
            else:
                checked = self.controller.db.updates_checked_at(r.package_manager.value)
                row = Adw.ActionRow(title="No update found",
                                    subtitle=esc(f"Last checked {format_date(checked, True)}" if checked
                                                 else "Not checked yet"))
                row.add_prefix(Gtk.Image.new_from_icon_name("emblem-ok-symbolic"))
            check = Gtk.Button(label="Check Now", valign=Gtk.Align.CENTER)
            check.set_action_name("win.app-check-update")
            check.set_action_target_value(GLib.Variant.new_string(r.key))
            row.add_suffix(check)
            group.add(row)
            self.content.append(group)
        else:
            group = Adw.PreferencesGroup(title="Updates")
            row = Adw.ActionRow(title="Updates are not managed by a package manager",
                                subtitle="Update it with its own updater or a new download — or link its GitHub "
                                         "repository below to be told about new releases.")
            row.add_prefix(Gtk.Image.new_from_icon_name("dialog-information-symbolic"))
            group.add(row)
            self.content.append(group)

        from .github_ui import details_group

        github = details_group(self.window, r)
        if github is not None:
            self.content.append(github)

        # Locations -------------------------------------------------------------------
        group = Adw.PreferencesGroup(title="Locations")
        for label, path in (("Installation path", r.install_path), ("Executable", r.exec_path),
                            ("Desktop entry", r.desktop_file)):
            if path:
                self._path_row(group, label, path)
        if r.exec_command:
            self._row(group, "Launch command", r.exec_command, mono=True)
        for path in r.related_paths:
            self._path_row(group, "Related file", path)
        data_dir = r.extra.get("data_dir")
        if data_dir and os.path.isdir(data_dir):
            self._path_row(group, "App data", data_dir)
        self.content.append(group)

        # Other installations -----------------------------------------------------------
        members = [m for m in inv.installations_of(r) if m.key != r.key]
        if members:
            group = Adw.PreferencesGroup(title="Other Installations",
                                         description="The same application was found through other sources. "
                                                     "Each installation can be managed separately.")
            for m in members:
                row = Adw.ActionRow(title=esc(m.name), subtitle=esc(f"{m.version} · {m.install_path}"),
                                    activatable=True)
                row.add_prefix(app_image(m, 32))
                row.add_suffix(source_badge(m.source))
                row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
                row.connect("activated", lambda _r, k=m.key: self.emit("show-app", k))
                group.add(row)
            self.content.append(group)

        # Review ----------------------------------------------------------------------------
        if r.package_manager == PackageManager.NONE:
            group = Adw.PreferencesGroup(title="Review",
                                         description="Tell the scanner what this item is. Your choice is saved "
                                                     "and used in future scans.")
            row = Adw.ActionRow(title="Classification", subtitle=esc(
                f"Currently: {r.classification.value.capitalize() if r.classification else 'not reviewed'}"))
            box = Gtk.Box(valign=Gtk.Align.CENTER)
            box.add_css_class("linked")
            for kind, label in ((Classification.APPLICATION, "Application"),
                                (Classification.COMPONENT, "Component"), (Classification.IGNORE, "Ignore")):
                b = Gtk.ToggleButton(label=label, active=r.classification == kind)
                b.connect("toggled", self._on_classify, kind)
                box.append(b)
            row.add_suffix(box)
            group.add(row)
            self.content.append(group)

        if self._can_measure():
            self._measure_size()

    def _can_measure(self) -> bool:
        r = self.record
        return r.size is None and r.package_manager == PackageManager.NONE and os.path.isdir(r.install_path or "")

    def _measure_size(self) -> None:
        path = self.record.install_path

        def worker():
            size = directory_size(path)
            GLib.idle_add(lambda: self.size_row.set_subtitle(esc(size_text(size) or "Unknown")) or False)

        threading.Thread(target=worker, daemon=True).start()

    def _on_classify(self, button: Gtk.ToggleButton, kind: Classification) -> None:
        if button.get_active():
            # Record the choice first so the siblings' "toggled" handlers see it.
            self.controller.classify(self.record, kind)
            child = button.get_parent().get_first_child()
            while child:
                if child is not button and isinstance(child, Gtk.ToggleButton):
                    child.set_active(False)
                child = child.get_next_sibling()
        elif self.record.classification == kind:
            self.controller.classify(self.record, None)

    def _row(self, group, title: str, value: str, mono: bool = False):
        row = Adw.ActionRow(title=esc(title), subtitle=esc(value), subtitle_selectable=True)
        row.add_css_class("property")
        if mono:
            row.add_css_class("monospace")
        group.add(row)
        return row

    def _path_row(self, group, title: str, path: str) -> None:
        exists = os.path.lexists(path)
        row = Adw.ActionRow(title=esc(title), subtitle=esc(path + ("" if exists else "  (missing)")),
                            subtitle_selectable=True)
        row.add_css_class("property")
        if exists:
            btn = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Show in Files")
            btn.add_css_class("flat")
            btn.connect("clicked", lambda *_: self.window.show_in_files(path))
            row.add_suffix(btn)
        copy = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Copy path")
        copy.add_css_class("flat")
        copy.connect("clicked", lambda *_: self.window.copy_text(path, "Path copied"))
        row.add_suffix(copy)
        group.add(row)
