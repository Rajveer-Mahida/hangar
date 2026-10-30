"""Package manager integrations: availability, versions, toggles and maintenance actions."""

from __future__ import annotations

import os
import threading

from gi.repository import Adw, GLib, GObject, Gtk

from ..core.application_model import PackageManager, Source
from ..core.scanner import all_providers
from ..services.inventory_service import AppController
from ..services.privilege_service import helper_status
from .widgets import LazyRefresh, clear_box, esc, pill

PROVIDER_SOURCES = {
    "apt": (PackageManager.APT, None),
    "snap": (PackageManager.SNAP, None),
    "flatpak": (PackageManager.FLATPAK, None),
    "appimage": (None, Source.APPIMAGE),
    "manual": (PackageManager.NONE, None),
}


class PackageManagersPage(Gtk.Box, LazyRefresh):
    __gtype_name__ = "UamPackageManagersPage"
    __gsignals__ = {
        "refresh-lists": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "remove-unused-runtimes": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, controller: AppController):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.controller = controller
        self.providers = all_providers()
        self.versions: dict[str, str] = {}
        self.available: dict[str, bool] = {}
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24, margin_top=24, margin_bottom=32,
                               margin_start=18, margin_end=18)
        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        sw.set_child(Adw.Clamp(maximum_size=860, child=self.content))
        self.append(sw)
        self.init_lazy_refresh()
        controller.connect("inventory-changed", self.queue_refresh)
        controller.connect("updates-changed", self.queue_refresh)
        self.refresh()
        self._probe()

    def _probe(self) -> None:
        def worker():
            versions, available = {}, {}
            for p in self.providers:
                try:
                    available[p.id] = p.available()
                    versions[p.id] = p.tool_version() if available[p.id] else ""
                except Exception as exc:  # shown to the user
                    available[p.id], versions[p.id] = False, str(exc)
            GLib.idle_add(self._probe_done, versions, available)

        threading.Thread(target=worker, daemon=True).start()

    def _probe_done(self, versions, available) -> bool:
        self.versions, self.available = versions, available
        self.queue_refresh()
        return False

    def refresh(self) -> None:
        clear_box(self.content)
        inv = self.controller.inventory
        settings = self.controller.settings
        status = inv.provider_status

        for provider in self.providers:
            pm, source = PROVIDER_SOURCES[provider.id]
            records = [r for r in inv.records.values()
                       if (pm is not None and r.package_manager == pm and (provider.id != "manual" or
                                                                           r.source != Source.APPIMAGE))
                       or (source is not None and r.source == source)]
            apps = [r for r in records if inv.is_listed(r)]
            components = [r for r in records if r.is_component]

            group = Adw.PreferencesGroup(title=provider.label, description=provider.description)
            state = status.get(provider.id)
            if provider.id in self.available and not self.available[provider.id]:
                badge = pill("Not installed", "warning")
            elif state == "disabled" or not settings.provider_enabled(provider.id):
                badge = pill("Disabled", "neutral")
            elif state == "error":
                badge = pill("Error", "error")
            elif state == "ok":
                badge = pill("Active", "success")
            else:
                badge = pill("Waiting for scan", "neutral")
            group.set_header_suffix(badge)

            version = self.versions.get(provider.id, "…")
            row = Adw.ActionRow(title="Version", subtitle=esc(version or "Unknown"))
            row.add_css_class("property")
            group.add(row)
            row = Adw.ActionRow(title="Detected", subtitle=esc(
                f"{len(apps)} application(s) listed · {len(records)} item(s) total"
                + (f" · {len(components)} component(s)/runtime(s)" if components else "")))
            row.add_css_class("property")
            group.add(row)
            if provider.supports_updates():
                checked = self.controller.db.updates_checked_at(provider.id)
                n = sum(1 for u in inv.updates.values() if u.source == provider.id)
                row = Adw.ActionRow(title="Updates", subtitle=esc(
                    f"{n} available · checked {GLib.DateTime.new_from_unix_local(int(checked)).format('%d %b %Y, %H:%M')}"
                    if checked else "Not checked yet"))
                row.add_css_class("property")
                group.add(row)

            switch = Adw.SwitchRow(title="Enable integration", active=settings.provider_enabled(provider.id))
            switch.set_subtitle("Include this source in scans and update checks")
            switch.connect("notify::active", self._on_toggle, provider.id)
            group.add(switch)

            if provider.id == "apt":
                row = Adw.ActionRow(title="Refresh package lists",
                                    subtitle="Runs 'apt-get update' as administrator so update checks see the "
                                             "newest versions")
                b = Gtk.Button(label="Refresh…", valign=Gtk.Align.CENTER)
                b.connect("clicked", lambda *_: self.emit("refresh-lists"))
                row.add_suffix(b)
                group.add(row)
            if provider.id == "flatpak":
                for installation in ("system", "user"):
                    row = Adw.ActionRow(title=f"Remove unused runtimes ({installation})",
                                        subtitle="Runtimes and extensions no installed app needs any more")
                    b = Gtk.Button(label="Clean Up…", valign=Gtk.Align.CENTER)
                    b.connect("clicked", lambda _b, i=installation: self.emit("remove-unused-runtimes", i))
                    row.add_suffix(b)
                    group.add(row)
            self.content.append(group)

        self.content.append(self._github_group())

        errors = inv.scan_errors
        if errors:
            group = Adw.PreferencesGroup(title="Scan warnings")
            for e in errors:
                row = Adw.ActionRow(title=esc(e))
                row.add_prefix(Gtk.Image.new_from_icon_name("dialog-warning-symbolic"))
                group.add(row)
            self.content.append(group)

        helper = helper_status()
        group = Adw.PreferencesGroup(
            title="Administrative access",
            description="Installing, updating and removing system software uses PolicyKit (pkexec) and a small "
                        "privileged helper that validates every request again. Inventory scanning never needs "
                        "administrator rights.")
        row = Adw.ActionRow(title="PolicyKit (pkexec)",
                            subtitle="Available" if helper["pkexec"] else "Not installed — administrative "
                                                                          "actions are unavailable")
        row.add_css_class("property")
        group.add(row)
        mode = {
            "installed": "Installed (root-owned helper with its own PolicyKit actions)",
            "development": "Development mode — the in-tree helper is run through pkexec (started with ./run.sh). "
                           "Install the app system-wide (scripts/install.sh) for a root-owned helper.",
            "unavailable": "Not installed — install, update and remove actions are unavailable until the app is "
                           "installed system-wide (scripts/install.sh or the .deb).",
        }[str(helper["mode"])]
        row = Adw.ActionRow(title="Privileged helper", subtitle=esc(mode))
        row.add_css_class("property")
        group.add(row)
        row = Adw.ActionRow(title="Helper location", subtitle=esc(str(helper["path"])), subtitle_selectable=True)
        row.add_css_class("property")
        group.add(row)
        self.content.append(group)

    def _github_group(self) -> Adw.PreferencesGroup:
        from ..services import github_service as gh

        inv, settings, db = self.controller.inventory, self.controller.settings, self.controller.db
        group = Adw.PreferencesGroup(
            title="GitHub releases",
            description="New-release checks for AppImages, manual installs and .deb files downloaded from GitHub. "
                        "Links come from AppImage update information or package homepages, or you set them in an "
                        "app's details.")
        enabled = settings.provider_enabled("github")
        group.set_header_suffix(pill("Active" if enabled else "Disabled", "success" if enabled else "neutral"))
        stored = db.github_links()
        trackable = [r for r in inv.records.values() if gh.is_trackable(r) and not r.hidden]
        linked = [r for r in trackable if gh.resolve_link(r, stored)]
        n_updates = sum(1 for u in inv.updates.values() if u.source == "github")
        checked = db.updates_checked_at("github")
        row = Adw.ActionRow(title="Tracked applications", subtitle=esc(
            f"{len(linked)} linked to a repository · {len(trackable) - len(linked)} not linked · "
            f"{n_updates} new release(s)"))
        row.add_css_class("property")
        group.add(row)
        row = Adw.ActionRow(title="Last check", subtitle=esc(
            GLib.DateTime.new_from_unix_local(int(checked)).format("%d %b %Y, %H:%M") if checked else "Not checked yet"))
        row.add_css_class("property")
        group.add(row)
        token = bool(os.environ.get("GITHUB_TOKEN"))
        row = Adw.ActionRow(title="API access", subtitle=esc(
            "Using GITHUB_TOKEN from the environment (higher rate limit)" if token else
            "Anonymous: 60 requests per hour, results are cached. Set GITHUB_TOKEN in the environment for more."))
        row.add_css_class("property")
        group.add(row)
        switch = Adw.SwitchRow(title="Enable integration", active=enabled)
        switch.connect("notify::active", self._on_toggle, "github")
        group.add(switch)
        return group

    def _on_toggle(self, row: Adw.SwitchRow, _pspec, provider_id: str) -> None:
        enabled = row.get_active()
        if enabled == self.controller.settings.provider_enabled(provider_id):
            return
        self.controller.settings.set_provider(provider_id, enabled)
        self.controller.log_action("settings", f"{provider_id} integration", "success",
                                   detail="enabled" if enabled else "disabled")
        self.controller.rescan("settings")
