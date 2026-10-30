"""Install software: search APT/Snap/Flatpak catalogues or install a local file."""

from __future__ import annotations

import os
import threading

from gi.repository import Adw, Gio, GLib, Gtk

from ..core.application_model import Source, format_size
from ..core.safety import ValidationError
from ..services import install_service
from ..services.install_service import Candidate
from ..services.privilege_service import PrivilegeError
from .widgets import clear_box, esc, gicon_for, pill, source_badge

SOURCES = [("all", "All sources"), ("apt", "APT"), ("flatpak", "Flatpak"), ("snap", "Snap")]
SOURCE_ENUM = {"apt": Source.APT, "snap": Source.SNAP, "flatpak": Source.FLATPAK}


class InstallDialog(Adw.Dialog):
    def __init__(self, window):
        super().__init__(title="Install Software", content_width=720, content_height=720)
        self.window = window
        self.controller = window.controller
        self._generation = 0

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        from_file = Gtk.Button(tooltip_text="Install a .deb, .flatpakref or AppImage file")
        from_file.set_child(Adw.ButtonContent(icon_name="document-open-symbolic", label="From File…"))
        from_file.connect("clicked", lambda *_: choose_file(self.window, after=self.close))
        header.pack_start(from_file)
        toolbar.add_top_bar(header)

        bar = Gtk.Box(spacing=8, margin_start=18, margin_end=18, margin_top=6, margin_bottom=6)
        self.entry = Gtk.SearchEntry(placeholder_text="Search for applications to install", hexpand=True)
        self.entry.set_search_delay(400)
        self.entry.connect("search-changed", lambda *_: self._search())
        bar.append(self.entry)
        self.source = Gtk.DropDown.new_from_strings([label for _, label in SOURCES])
        self.source.connect("notify::selected", lambda *_: self._search())
        bar.append(self.source)
        toolbar.add_top_bar(bar)

        self.stack = Gtk.Stack(vexpand=True)
        self.status = Adw.StatusPage(icon_name="system-software-install-symbolic", title="Find Software",
                                     description="Search the APT, Flatpak and Snap catalogues. Nothing is installed "
                                                 "without your confirmation.")
        self.stack.add_named(self.status, "status")
        loading = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        loading.append(Gtk.Spinner(spinning=True))
        self.loading_label = Gtk.Label(label="Searching…")
        loading.append(self.loading_label)
        self.stack.add_named(loading, "loading")
        self.results = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.results.add_css_class("boxed-list")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_start=18, margin_end=18,
                      margin_top=12, margin_bottom=18)
        self.errors = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(self.errors)
        box.append(self.results)
        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.set_child(box)
        self.stack.add_named(sw, "results")
        toolbar.set_content(self.stack)
        self.set_child(toolbar)
        self.set_focus(self.entry)

    def _sources(self) -> list[str]:
        key = SOURCES[self.source.get_selected()][0]
        enabled = [s for s in ("apt", "flatpak", "snap") if self.controller.settings.provider_enabled(s)]
        return enabled if key == "all" else [s for s in enabled if s == key]

    def _search(self) -> None:
        query = self.entry.get_text().strip()
        self._generation += 1
        generation = self._generation
        if len(query) < 2:
            self.stack.set_visible_child_name("status")
            return
        self.loading_label.set_label(f"Searching for “{query}”…")
        self.stack.set_visible_child_name("loading")
        records = list(self.controller.inventory.records.values())
        sources = self._sources()

        def worker():
            results, errors = install_service.search(query, sources, records)
            GLib.idle_add(self._show, generation, results, errors)

        threading.Thread(target=worker, daemon=True).start()

    def _show(self, generation: int, results: list[Candidate], errors: dict[str, str]) -> bool:
        if generation != self._generation:
            return False  # a newer search is running
        clear_box(self.results)
        clear_box(self.errors)
        for text in errors.values():
            label = Gtk.Label(label=text, xalign=0, wrap=True)
            label.add_css_class("note-box")
            self.errors.append(label)
        if not results:
            self.status.set_title("No Results")
            self.status.set_description("Try another search term or source.")
            self.stack.set_visible_child_name("status" if not errors else "results")
            return False
        for cand in results:
            self.results.append(self._row(cand))
        self.stack.set_visible_child_name("results")
        return False

    def _row(self, cand: Candidate) -> Adw.ActionRow:
        subtitle = cand.summary
        meta = [m for m in (cand.version, cand.origin) if m]
        if meta:
            subtitle += "\n" + " · ".join(meta)
        row = Adw.ActionRow(title=esc(cand.name), subtitle=esc(subtitle), subtitle_lines=2)
        image = Gtk.Image(pixel_size=40)
        image.set_from_gicon(gicon_for(cand.icon) if cand.icon else
                             Gio.ThemedIcon.new("application-x-executable"))
        row.add_prefix(image)
        badges = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        badges.append(source_badge(SOURCE_ENUM[cand.source]))
        if cand.classic:
            badges.append(pill("Classic", "warning"))
        row.add_suffix(badges)
        if cand.installed:
            row.add_suffix(pill("Installed", "success"))
        else:
            button = Gtk.Button(label="Install", valign=Gtk.Align.CENTER)
            button.add_css_class("suggested-action")
            button.connect("clicked", lambda *_: confirm_install(self.window, cand, after=self.close))
            row.add_suffix(button)
        return row


# ---------------------------------------------------------------------------
# Confirmation flows
# ---------------------------------------------------------------------------

def _alert(window, heading: str, body: str, action: str, on_ok, destructive=False) -> None:
    dialog = Adw.AlertDialog(heading=heading, body=body)
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("ok", action)
    dialog.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE if destructive
                                   else Adw.ResponseAppearance.SUGGESTED)
    dialog.set_close_response("cancel")
    dialog.connect("response", lambda _d, r: on_ok() if r == "ok" else None)
    dialog.present(window)


def _submit(window, make_op, after=None) -> None:
    try:
        window.controller.submit(make_op())
        if after:
            after()
    except (PrivilegeError, ValidationError, ValueError) as exc:
        window.show_error("Installation could not be started", str(exc))


def confirm_install(window, cand: Candidate, after=None) -> None:
    if cand.source == "apt":
        window.toast(f"Preparing {cand.name}…")

        def worker():
            packages, removed, error = install_service.simulate_apt_install(cand.identifier)
            GLib.idle_add(show, packages, removed, error)

        def show(packages, removed, error):
            if error:
                window.show_error(f"Cannot install {cand.name}", error)
                return False
            if removed:
                window.show_error(f"Cannot install {cand.name}",
                                  "Installing it would make APT remove other packages: " + ", ".join(removed[:15])
                                  + ".\n\nThis app never removes packages as a side effect of an installation. "
                                    "Remove the conflicting packages first if you really want to switch.")
                return False
            extra = [p for p in packages if p != cand.identifier]
            body = f"Package: {cand.identifier}"
            if extra:
                body += (f"\n\nAPT also installs {len(extra)} dependenc{'y' if len(extra) == 1 else 'ies'}: "
                         + ", ".join(extra[:15]) + (" …" if len(extra) > 15 else ""))
            body += "\n\nAdministrator authentication is required."
            _alert(window, f"Install {cand.name}?", body, "Install",
                   lambda: _submit(window, lambda: install_service.install_operation(cand), after))
            return False

        threading.Thread(target=worker, daemon=True).start()
        return
    if cand.source == "snap":
        body = f"Snap: {cand.identifier}"
        if cand.origin:
            body += f"\nPublisher: {cand.origin}"
        if cand.download_size:
            body += f"\nDownload: {format_size(cand.download_size)}"
        if cand.classic:
            body += ("\n\nThis snap uses classic confinement: it is not sandboxed and has full access to your "
                     "system, like traditionally packaged software. Only continue if you trust the publisher.")
        body += "\n\nAdministrator authentication is required."
        _alert(window, f"Install {cand.name}?", body, "Install",
               lambda: _submit(window, lambda: install_service.install_operation(cand), after),
               destructive=cand.classic)
        return
    if cand.source == "flatpak":
        body = (f"Flatpak: {cand.identifier}\nRemote: {cand.remote or 'detected automatically'}\n\n"
                "The app is installed system-wide and runs in a sandbox. Required runtimes are downloaded "
                "automatically. Administrator authentication is required.")
        _alert(window, f"Install {cand.name}?", body, "Install",
               lambda: _submit(window, lambda: install_service.install_operation(cand), after))


def choose_file(window, after=None) -> None:
    dialog = Gtk.FileDialog(title="Install from File", modal=True)
    filters = Gio.ListStore.new(Gtk.FileFilter)
    combined = Gtk.FileFilter(name="Installable files (.deb, .flatpakref, AppImage)")
    for pattern in ("*.deb", "*.flatpakref", "*.AppImage", "*.appimage"):
        combined.add_pattern(pattern)
    filters.append(combined)
    dialog.set_filters(filters)
    dialog.set_default_filter(combined)

    def done(d, result):
        try:
            file = d.open_finish(result)
        except GLib.Error:
            return
        path = file.get_path() if file else None
        if path:
            if after:
                after()
            install_file(window, path)

    dialog.open(window, None, done)


def install_file(window, path: str) -> None:
    path = os.path.realpath(path)
    lower = path.lower()
    try:
        if lower.endswith(".deb"):
            _install_deb(window, path)
        elif lower.endswith(".flatpakref"):
            info = install_service.flatpakref_info(path)
            body = (f"Application: {info['name']} ({info['branch']})\nSource: {info['url'] or 'unknown'}\n\n"
                    "Installing from a .flatpakref may add its repository to your system. Only continue if you "
                    "trust where the file came from. Administrator authentication is required.")
            _alert(window, f"Install {info['title']}?", body, "Install",
                   lambda: _submit(window, lambda: install_service.flatpakref_install_operation(
                       path, info["title"], info["sha256"])))
        elif install_service.appimage_type(path) or lower.endswith(".appimage"):
            plan = install_service.appimage_install_plan(path)
            body = (f"The AppImage is {'kept in' if plan['source'] == plan['target'] else 'copied to'} "
                    f"{os.path.dirname(plan['target'])}, marked as executable, and a launcher is added to your "
                    "applications menu.\n\nAppImages are not sandboxed and run with your permissions. Only "
                    "continue if you trust the source. No administrator rights are needed.")
            _alert(window, f"Install {plan['name']}?", body, "Install",
                   lambda: _submit(window, lambda: install_service.appimage_install_operation(plan)))
        else:
            window.show_error("Unsupported file", "Choose a .deb package, a .flatpakref file or an AppImage.")
    except (ValidationError, OSError) as exc:
        window.show_error("Cannot install this file", str(exc))


def _install_deb(window, path: str) -> None:
    window.toast("Reading package…")

    def worker():
        try:
            info = install_service.deb_info(path)
            packages, removed, error = install_service.simulate_apt_install(path)
        except (ValidationError, OSError) as exc:
            info, packages, removed, error = {}, [], [], str(exc)
        GLib.idle_add(show, info, packages, removed, error)

    def show(info, packages, removed, error):
        if error:
            window.show_error("Cannot install this package", error)
            return False
        if removed:
            window.show_error("Cannot install this package",
                              "Installing it would make APT remove other packages: " + ", ".join(removed[:15])
                              + ".\n\nThis app never removes packages as a side effect of an installation.")
            return False
        name = info.get("Package", os.path.basename(path))
        maintainer = info.get("Maintainer", "unknown")
        summary = info.get("Description", "").split("\n")[0]
        extra = [p for p in packages if p != name]
        body = f"{summary}\n\nPackage: {name} {info.get('Version', '')}\nMaintainer: {maintainer}"
        if extra:
            body += f"\n\nAPT also installs: {', '.join(extra[:15])}" + (" …" if len(extra) > 15 else "")
        body += ("\n\nPackages from outside your configured repositories can run code as administrator during "
                 "installation and do not receive updates unless they add their own repository. Only continue "
                 "if you trust the vendor. Administrator authentication is required.")
        _alert(window, f"Install {name}?", body, "Install",
               lambda: _submit(window, lambda: install_service.deb_install_operation(path, name, info["SHA256"])))
        return False

    threading.Thread(target=worker, daemon=True).start()
