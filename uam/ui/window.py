"""Main window: sidebar navigation, section pages, app actions."""

from __future__ import annotations

import os
import threading

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from .. import APP_NAME, APP_SHORT_NAME
from ..core.application_model import AppRecord, Classification, PackageManager, Source
from ..services.inventory_service import AppController
from ..services.operation_service import Operation
from ..services.privilege_service import PrivilegeError
from ..services.uninstall_service import build_plan, operations_for
from ..services.update_service import (
    operations_for_updates,
    refresh_apt_lists_operation,
    remove_unused_flatpak_runtimes_operation,
)
from .applications_page import ApplicationsPage
from .dashboard import DashboardPage
from .details_page import DetailsPage
from .dialogs import BulkUninstallDialog, UninstallDialog, confirm_action, confirm_updates
from .history_page import HistoryPage
from . import github_ui
from .install_dialog import InstallDialog, choose_file, install_file
from .manual_page import ManualPage
from .operation_bar import OperationBar
from .package_managers_page import PackageManagersPage
from .settings_page import SettingsPage
from .sidebar import Sidebar
from .updates_page import UpdatesPage
from .widgets import relative_time


class MainWindow(Adw.ApplicationWindow):
    __gtype_name__ = "UamMainWindow"

    def __init__(self, app: Adw.Application, controller: AppController):
        super().__init__(application=app, title=APP_NAME, default_width=1220, default_height=800)
        self.set_size_request(440, 480)
        self.controller = controller
        self.current_section = "overview"

        self.toasts = Adw.ToastOverlay()
        self.split = Adw.NavigationSplitView(min_sidebar_width=220, max_sidebar_width=280, sidebar_width_fraction=0.22)

        # Sidebar -----------------------------------------------------------------
        self.sidebar = Sidebar()
        self.sidebar.connect("section-selected", lambda _s, sid: self.show_section(sid))
        sidebar_toolbar = Adw.ToolbarView()
        sidebar_header = Adw.HeaderBar()
        sidebar_header.set_title_widget(Adw.WindowTitle(title=APP_SHORT_NAME, subtitle=""))
        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=self._primary_menu(),
                                     tooltip_text="Main menu", primary=True)
        sidebar_header.pack_end(menu_button)
        sidebar_toolbar.add_top_bar(sidebar_header)
        sidebar_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        sidebar_scroll.set_child(self.sidebar)
        sidebar_toolbar.set_content(sidebar_scroll)
        footer = Gtk.Box(spacing=8, margin_start=14, margin_end=14, margin_top=8, margin_bottom=10)
        self.scan_spinner = Gtk.Spinner()
        footer.append(self.scan_spinner)
        self.scan_label = Gtk.Label(xalign=0, ellipsize=3, hexpand=True)
        self.scan_label.add_css_class("dim-label")
        self.scan_label.add_css_class("caption")
        footer.append(self.scan_label)
        sidebar_toolbar.add_bottom_bar(footer)
        self.split.set_sidebar(Adw.NavigationPage(title=APP_NAME, child=sidebar_toolbar))

        # Content -----------------------------------------------------------------
        self.nav = Adw.NavigationView()
        self.operation_bar = OperationBar(controller.operations)
        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content_box.append(self.nav)
        self.nav.set_vexpand(True)
        content_box.append(self.operation_bar)
        self.content_page = Adw.NavigationPage(title="Overview", child=content_box)
        self.split.set_content(self.content_page)

        self.pages: dict[str, Adw.NavigationPage] = {}
        self.dashboard = DashboardPage(controller)
        self.dashboard.connect("navigate", self._on_navigate)
        self.dashboard.connect("show-app", lambda _w, key: self.show_app(key))
        self.applications = ApplicationsPage(controller)
        self.applications.connect("show-app", lambda _w, key: self.show_app(key))
        self.applications.connect("uninstall-apps", lambda _w, records: self.uninstall(records))
        self.applications.connect("update-apps", lambda _w, records: self.update_records(records))
        self.updates = UpdatesPage(controller)
        self.updates.connect("update-request", lambda _w, updates, every: self.request_updates(updates, every))
        self.updates.connect("refresh-lists", lambda *_: self.refresh_lists())
        self.updates.connect("github-update", lambda _w, update: github_ui.start_update(self, update))
        self.manual = ManualPage(controller)
        self.manual.connect("show-app", lambda _w, key: self.show_app(key))
        self.manual.connect("open-settings", lambda *_: self.show_section("settings"))
        self.managers = PackageManagersPage(controller)
        self.managers.connect("refresh-lists", lambda *_: self.refresh_lists())
        self.managers.connect("remove-unused-runtimes", lambda _w, inst: self.remove_unused_runtimes(inst))
        self.history = HistoryPage(controller, self)
        self.settings_page = SettingsPage(controller, self)

        for section_id, widget in (("overview", self.dashboard), ("applications", self.applications),
                                   ("updates", self.updates), ("manual", self.manual),
                                   ("managers", self.managers), ("history", self.history),
                                   ("settings", self.settings_page)):
            self.pages[section_id] = self._wrap(section_id, widget)

        self.toasts.set_child(self.split)
        self.set_content(self.toasts)

        breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 760sp"))
        breakpoint.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(breakpoint)
        # Narrow windows: icon-only filter toggles, no counters in the filter bar.
        # Only one breakpoint applies at a time, so this one repeats the collapse.
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 720sp"))
        narrow.add_setter(self.split, "collapsed", True)
        narrow.add_setter(self.applications.updates_content, "label", "")
        narrow.add_setter(self.applications.manual_content, "label", "")
        narrow.add_setter(self.applications.count_label, "visible", False)
        narrow.add_setter(self.applications.sort_label, "visible", False)
        narrow.add_setter(self.dashboard.rescan_content, "label", "")
        narrow.add_setter(self.dashboard.install_content, "label", "")
        self.add_breakpoint(narrow)

        self._install_actions()
        controller.connect("inventory-changed", lambda *_: self._update_counts())
        controller.connect("updates-changed", lambda *_: self._update_counts())
        controller.connect("scan-state", self._on_scan_state)
        controller.connect("notify-user", lambda _c, text: self.toast(text))
        controller.operations.connect("operation-finished", self._on_operation_finished)
        self.nav.add(self.pages["overview"])
        self.sidebar.select_section("overview")
        self._update_counts()
        GLib.timeout_add_seconds(30, self._tick)

    # -- construction helpers ---------------------------------------------------------
    def _wrap(self, section_id: str, widget: Gtk.Widget) -> Adw.NavigationPage:
        title = self.sidebar.title_for(section_id)
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        title_widget = getattr(widget, "header_title", None)
        header.set_title_widget(title_widget or Adw.WindowTitle(title=title, subtitle=""))
        for w in getattr(widget, "header_start", []):
            header.pack_start(w)
        for w in getattr(widget, "header_end", []):
            header.pack_end(w)
        toolbar.add_top_bar(header)
        if getattr(widget, "top_bar", None) is not None:
            toolbar.add_top_bar(widget.top_bar)
        toolbar.set_content(widget)
        if getattr(widget, "bottom_bar", None) is not None:
            toolbar.add_bottom_bar(widget.bottom_bar)
        return Adw.NavigationPage(title=title, tag=section_id, child=toolbar)

    def _primary_menu(self) -> Gio.Menu:
        menu = Gio.Menu()
        section = Gio.Menu()
        section.append("Install Software…", "win.install")
        section.append("Install from File…", "win.install-file")
        menu.append_section(None, section)
        section = Gio.Menu()
        section.append("Rescan Inventory", "win.rescan")
        section.append("Check for Updates", "win.check-updates")
        section.append("Refresh APT Package Lists…", "win.refresh-lists")
        menu.append_section(None, section)
        section = Gio.Menu()
        section.append("Settings", "win.show-settings")
        section.append(f"About {APP_NAME}", "app.about")
        menu.append_section(None, section)
        return menu

    def _install_actions(self) -> None:
        simple = {
            "rescan": lambda: self.controller.rescan("user"),
            "check-updates": lambda: self.controller.check_updates(quiet=False),
            "refresh-lists": self.refresh_lists,
            "show-settings": lambda: self.show_section("settings"),
            "search": self._focus_search,
            "install": lambda: InstallDialog(self).present(self),
            "install-file": lambda: choose_file(self),
        }
        for name, callback in simple.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, cb=callback: cb())
            self.add_action(action)
        per_app = {
            "app-launch": self.launch,
            "app-details": lambda r: self.show_app(r.key),
            "app-locate": self.locate,
            "app-copy": lambda r: self.copy_text(self._info_text(r), "Package information copied"),
            "app-check-update": self.check_update,
            "app-update": lambda r: self.update_records([r]),
            "app-uninstall": lambda r: self.uninstall([r]),
        }
        for name, callback in per_app.items():
            action = Gio.SimpleAction.new(name, GLib.VariantType.new("s"))
            action.connect("activate", self._on_app_action, callback)
            self.add_action(action)
        classify = Gio.SimpleAction.new("app-classify", GLib.VariantType.new("s"))
        classify.connect("activate", self._on_classify)
        self.add_action(classify)
        app = self.get_application()
        app.set_accels_for_action("win.rescan", ["<Control>r", "F5"])
        app.set_accels_for_action("win.search", ["<Control>f"])
        app.set_accels_for_action("win.show-settings", ["<Control>comma"])
        app.set_accels_for_action("win.install", ["<Control>i"])

    def _on_classify(self, _action, param) -> None:
        kind, _, key = param.get_string().partition("|")
        record = self.controller.inventory.records.get(key)
        if record is None:
            return
        value = None if kind == "none" else Classification(kind)
        if record.classification != value:
            self.controller.classify(record, value)
            self.toast(f"{record.name}: " + (f"marked as {kind}" if value else "review cleared"))

    def _on_app_action(self, _action, param, callback) -> None:
        record = self.controller.inventory.records.get(param.get_string())
        if record is None:
            self.toast("This item is no longer in the inventory")
            return
        callback(record)

    # -- navigation ----------------------------------------------------------------------
    def show_section(self, section_id: str) -> None:
        page = self.pages[section_id]
        self.current_section = section_id
        self.sidebar.select_section(section_id)
        self.nav.replace([page])
        self.content_page.set_title(page.get_title())
        self.split.set_show_content(True)

    def _on_navigate(self, _widget, section_id: str, arg: str) -> None:
        if section_id == "applications":
            self.applications.reset_filters()
            if arg:
                self.applications.set_source_filter(arg)
        self.show_section(section_id)

    def _focus_search(self) -> None:
        if self.current_section != "applications" or self.nav.get_visible_page() is not self.pages["applications"]:
            self.show_section("applications")
        self.applications.search.grab_focus()

    def show_app(self, key: str) -> None:
        record = self.controller.inventory.records.get(key)
        if record is None:
            self.toast("This item is no longer in the inventory")
            return
        page = DetailsPage(self.controller, record, self)
        page.connect("show-app", lambda _p, k: self.show_app(k))
        self.nav.push(page)
        self.split.set_show_content(True)

    # -- status ----------------------------------------------------------------------------
    def _update_counts(self) -> None:
        inv = self.controller.inventory
        counts = inv.counts()
        self.sidebar.set_count("applications", len(inv.listed_records()))
        self.sidebar.set_count("updates", counts["updates"], accent=True)
        self.sidebar.set_count("manual", counts["needs_review"] or None, accent=bool(counts["needs_review"]))
        self._tick()

    def _on_scan_state(self, _c, running: bool, message: str, _fraction: float) -> None:
        self.scan_spinner.set_spinning(running)
        self.scan_spinner.set_visible(running)
        if running:
            self.scan_label.set_label(message)
        else:
            self._tick()
            if message.startswith("Scan failed"):
                self.toast(message)

    def _tick(self) -> bool:
        if not self.controller.scanning:
            last = self.controller.inventory.last_scan
            self.scan_label.set_label(f"Inventory updated {relative_time(last)}" if last else "Not scanned yet")
        return True

    def open_files(self, paths: list[str]) -> None:
        """Install files passed on the command line or via 'Open With'."""
        for path in paths:
            install_file(self, path)

    def toast(self, text: str, timeout: int = 3) -> Adw.Toast:
        toast = Adw.Toast(title=GLib.markup_escape_text(text), timeout=timeout)
        self.toasts.add_toast(toast)
        return toast

    def show_error(self, heading: str, body: str, output: list[str] | None = None) -> None:
        dialog = Adw.AlertDialog(heading=heading, body=body)
        if output:
            # Keep the command output with the error: the operation bar moves on to the next
            # queued operation, so its log is gone by the time the user opens this dialog.
            view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                                wrap_mode=Gtk.WrapMode.WORD_CHAR)
            view.add_css_class("operation-log")
            view.get_buffer().set_text("\n".join(output[-200:]))
            scroller = Gtk.ScrolledWindow(min_content_height=180, max_content_height=320,
                                          propagate_natural_height=True, child=view)
            expander = Gtk.Expander(label="Command output", child=scroller)

            def scroll_to_end(*_args) -> bool:
                buf = view.get_buffer()
                view.scroll_mark_onscreen(buf.create_mark(None, buf.get_end_iter(), False))
                return False

            expander.connect("notify::expanded", lambda *_: GLib.idle_add(scroll_to_end))
            dialog.set_extra_child(expander)
        dialog.add_response("close", "Close")
        dialog.present(self)

    def _on_operation_finished(self, _manager, op: Operation) -> None:
        if op.status == "success" and op.kind == "remove":
            # Leave detail pages of software that no longer exists.
            removed = {f"details:{key}" for key in op.app_keys}
            page = self.nav.get_visible_page()
            while page is not None and page.get_tag() in removed and self.nav.pop():
                page = self.nav.get_visible_page()
        if op.status == "success":
            verbs = {"remove": "removed", "update": "updated", "install": "installed", "refresh": "refreshed",
                     "cleanup": "cleaned up"}
            self.toast(f"{', '.join(op.targets)[:80]} {verbs.get(op.kind, 'done')}")
        elif op.status == "failed":
            toast = self.toast(f"{op.title} failed", timeout=8)
            toast.set_button_label("Details")
            toast.connect("button-clicked", lambda *_: self.show_error(f"{op.title} failed", op.error, list(op.log)))

    # -- app actions -------------------------------------------------------------------------
    def launch(self, record: AppRecord) -> None:
        desktop = record.desktop_file
        if desktop and os.path.exists(desktop):
            info = Gio.DesktopAppInfo.new_from_filename(desktop)
            if info is not None:
                try:
                    info.launch([], self.get_display().get_app_launch_context())
                    self.controller.log_action("launch", record.name, "success", record.source.label)
                    self.toast(f"Opening {record.name}")
                except GLib.Error as exc:
                    self.show_error(f"Could not open {record.name}", exc.message)
                return
        if record.source == Source.APPIMAGE and record.exec_path:
            if not os.access(record.exec_path, os.X_OK):
                self.show_error(f"Could not open {record.name}",
                                "The AppImage is not marked as executable. Make it executable in its file "
                                "properties if you trust it.")
                return
            try:
                Gio.Subprocess.new([record.exec_path], Gio.SubprocessFlags.NONE)
                self.controller.log_action("launch", record.name, "success", record.source.label)
                self.toast(f"Opening {record.name}")
            except GLib.Error as exc:
                self.show_error(f"Could not open {record.name}", exc.message)
            return
        cmd = os.path.basename(record.exec_path) if record.exec_path else record.package_name
        self.show_error(f"{record.name} has no graphical launcher",
                        f"This looks like a command-line program. Open a terminal and run '{cmd}'.")

    def locate(self, record: AppRecord) -> None:
        path = record.install_path
        if record.package_manager == PackageManager.APT and record.exec_path:
            path = os.path.realpath(record.exec_path)
        if not path or not os.path.lexists(path):
            path = record.desktop_file
        if not path:
            self.toast("The installation location is unknown")
            return
        self.show_in_files(path)

    def show_in_files(self, path: str) -> None:
        if not os.path.lexists(path):
            self.toast(f"{path} does not exist")
            return
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(path))

        def done(l, result, open_folder):
            try:
                (l.launch_finish if open_folder else l.open_containing_folder_finish)(result)
            except GLib.Error as exc:
                if not (exc.domain == "gtk-dialog-error-quark" and exc.code == int(Gtk.DialogError.DISMISSED)):
                    self.toast(f"Could not open the file manager: {exc.message}")

        if os.path.isdir(path) and not os.path.islink(path):
            launcher.launch(self, None, done, True)
        else:
            launcher.open_containing_folder(self, None, done, False)

    def copy_text(self, text: str, message: str) -> None:
        self.get_clipboard().set_content(Gdk.ContentProvider.new_for_value(text))
        self.toast(message)

    def _info_text(self, record: AppRecord) -> str:
        text = record.info_text()
        update = self.controller.inventory.update_for(record)
        if update:
            text += f"\nUpdate available: {update.new_version}"
        return text

    def check_github(self, record: AppRecord) -> None:
        if not self.controller.settings.provider_enabled("github"):
            self.toast("GitHub release checks are turned off in Settings")
            return
        handler = 0

        def done(_c, running):
            nonlocal handler
            if running:
                return
            self.controller.disconnect(handler)
            update = self.controller.inventory.update_for(record)
            error = self.controller.update_errors.get("github")
            if update is not None and update.source == "github":
                self.toast(f"{record.name}: version {update.new_version} is available on GitHub")
            elif error:
                self.toast(f"GitHub check failed: {error}", timeout=6)
            else:
                self.toast(f"{record.name}: no newer GitHub release found")
            self.reload_details(record.key)

        handler = self.controller.connect("update-check-state", done)
        self.toast("Checking GitHub releases…")
        self.controller.check_updates(["github"], quiet=True)

    def reload_details(self, key: str) -> None:
        page = self.nav.get_visible_page()
        if page is not None and page.get_tag() == f"details:{key}" and self.nav.pop():
            self.show_app(key)

    def check_update(self, record: AppRecord) -> None:
        source = record.package_manager.value
        # Downloaded .debs, AppImages and manual installs: only GitHub can tell.
        if github_ui.gh.is_trackable(record):
            if github_ui.gh.resolve_link(record, self.controller.db.github_links()):
                self.check_github(record)
            else:
                self.toast("Link this app to its GitHub repository (in its details) to check for new releases")
            return
        handler = 0

        def done(_c, running):
            nonlocal handler
            if running:
                return
            self.controller.disconnect(handler)
            update = self.controller.inventory.update_for(record)
            self.toast(f"{record.name}: version {update.new_version} is available" if update
                       else f"{record.name} is up to date")

        handler = self.controller.connect("update-check-state", done)
        self.toast(f"Checking {record.package_manager.label} for updates…")
        self.controller.check_updates([source], quiet=True)

    def update_records(self, records: list[AppRecord]) -> None:
        updates = []
        seen = set()
        for r in records:
            u = self.controller.inventory.update_for(r)
            if u and u.key not in seen:
                seen.add(u.key)
                updates.append(u)
        if not updates:
            self.toast("No updates available for the selection")
            return
        github = [u for u in updates if u.source == "github"]
        others = [u for u in updates if u.source != "github"]
        if others:
            self.request_updates(others, False)
        if github:
            github_ui.start_update(self, github[0])
            if len(github) > 1:
                self.toast("GitHub release updates are downloaded one at a time", timeout=5)

    def request_updates(self, updates, everything_apt: bool) -> None:
        updates = [u for u in updates if u.source != "github"]
        if not updates:
            return

        def go():
            try:
                for op in operations_for_updates(updates, everything_apt):
                    self.controller.submit(op)
            except (PrivilegeError, ValueError) as exc:
                self.show_error("Updates could not be started", str(exc))

        confirm_updates(self, updates, everything_apt, go)

    def refresh_lists(self) -> None:
        def go():
            try:
                self.controller.submit(refresh_apt_lists_operation())
            except PrivilegeError as exc:
                self.show_error("Could not refresh package lists", str(exc))

        confirm_action(self, "Refresh APT Package Lists?",
                       "Downloads the latest package information from your configured repositories "
                       "('apt-get update'). Administrator authentication is required. No software is "
                       "installed or removed.", "Refresh", go)

    def remove_unused_runtimes(self, installation: str) -> None:
        def go():
            try:
                self.controller.submit(remove_unused_flatpak_runtimes_operation(installation))
            except (PrivilegeError, ValueError) as exc:
                self.show_error("Could not start the clean-up", str(exc))

        body = ("Flatpak removes runtimes and extensions that no installed application uses any more "
                f"({installation} installation). Installed applications are not affected.")
        if installation == "system":
            body += " Administrator authentication is required."
        confirm_action(self, "Remove Unused Runtimes?", body, "Remove", go, destructive=True)

    def uninstall(self, records: list[AppRecord]) -> None:
        if not records:
            return
        if len(records) > 1:
            BulkUninstallDialog(self, records).present(self)
            return
        record = records[0]
        home = os.path.expanduser("~") + "/"
        reversible = record.package_manager == PackageManager.NONE and record.install_path.startswith(home)
        if self.controller.settings.confirm_uninstall or not reversible:
            UninstallDialog(self, record).present(self)
            return
        # Confirmation disabled: reversible Trash-only removals go straight ahead,
        # anything else still shows the preview.
        records_all = list(self.controller.inventory.records.values())

        def worker():
            plan = build_plan(record, records_all)
            GLib.idle_add(self._quick_uninstall, record, plan)

        threading.Thread(target=worker, daemon=True).start()

    def _quick_uninstall(self, record, plan) -> bool:
        if plan.supported and not plan.requires_root and not plan.warnings:
            for op in operations_for(plan):
                self.controller.submit(op)
            self.toast(f"Moving {record.name} to the Trash")
        else:
            UninstallDialog(self, record, plan).present(self)
        return False
