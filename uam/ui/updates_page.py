"""Updates from APT, Snap and Flatpak."""

from __future__ import annotations

from gi.repository import Adw, GObject, Gtk

from ..core.application_model import UpdateInfo
from ..package_managers.apt import apt_lists_age
from ..services.inventory_service import AppController
from ..services.update_service import BATCH_SOURCES, SOURCE_LABELS, UPDATE_SOURCES
from .widgets import LazyRefresh, app_image, clear_box, esc, pill, relative_time, size_text


class UpdatesPage(Gtk.Box, LazyRefresh):
    __gtype_name__ = "UamUpdatesPage"
    __gsignals__ = {
        "update-request": (GObject.SignalFlags.RUN_FIRST, None, (object, bool)),
        "github-update": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "refresh-lists": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, controller: AppController):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.controller = controller
        self.selected: set[str] = set()

        self.check_button = Gtk.Button()
        self.check_content = Adw.ButtonContent(icon_name="view-refresh-symbolic", label="Check for Updates")
        self.check_button.set_child(self.check_content)
        self.check_button.connect("clicked", lambda *_: controller.check_updates(quiet=False))
        self.spinner = Gtk.Spinner()
        refresh_lists = Gtk.Button(icon_name="emblem-synchronizing-symbolic",
                                   tooltip_text="Refresh APT package lists (requires authentication)")
        refresh_lists.connect("clicked", lambda *_: self.emit("refresh-lists"))
        self.header_end = [self.check_button, self.spinner, refresh_lists]

        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=22, margin_top=24, margin_bottom=32,
                               margin_start=18, margin_end=18)
        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        sw.set_child(Adw.Clamp(maximum_size=900, child=self.content))
        self.append(sw)

        self.action_bar = Gtk.ActionBar(revealed=False)
        self.selected_label = Gtk.Label()
        self.action_bar.pack_start(self.selected_label)
        self.update_selected = Gtk.Button(label="Update Selected")
        self.update_selected.add_css_class("suggested-action")
        self.update_selected.connect("clicked", lambda *_: self._request(self._selected_updates(), False))
        self.action_bar.pack_end(self.update_selected)
        self.bottom_bar = self.action_bar

        self.init_lazy_refresh()
        controller.connect("updates-changed", self.queue_refresh)
        controller.connect("update-check-state", self._on_check_state)
        self.refresh()

    def _on_check_state(self, _c, running: bool) -> None:
        self.queue_refresh()
        self.check_button.set_sensitive(not running)
        self.spinner.set_spinning(running)
        self.spinner.set_visible(running)
        self.check_content.set_label("Checking…" if running else "Check for Updates")

    def _request(self, updates: list[UpdateInfo], everything_apt: bool) -> None:
        if updates:
            self.emit("update-request", updates, everything_apt)

    def _selected_updates(self) -> list[UpdateInfo]:
        return [u for k, u in self.controller.inventory.updates.items() if k in self.selected]

    def _toggle(self, key: str, active: bool) -> None:
        if active:
            self.selected.add(key)
        else:
            self.selected.discard(key)
        self._update_action_bar()

    def _update_action_bar(self) -> None:
        n = len(self.selected)
        self.action_bar.set_revealed(n > 0)
        self.selected_label.set_label(f"{n} selected")

    # -------------------------------------------------------------------------
    def refresh(self) -> None:
        clear_box(self.content)
        inv = self.controller.inventory
        updates = list(inv.updates.values())
        self.selected &= {u.key for u in updates}
        self._update_action_bar()
        db = self.controller.db

        # Summary card
        card = Gtk.Box(spacing=16)
        card.add_css_class("card")
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True, margin_top=18,
                        margin_bottom=18, margin_start=20)
        apps = [u for u in updates if u.is_application]
        enabled = [s for s in UPDATE_SOURCES if self.controller.settings.provider_enabled(s)]
        never_checked = all(db.updates_checked_at(s) is None for s in enabled)
        title = Gtk.Label(xalign=0, wrap=True)
        title.add_css_class("title-2")
        if updates:
            title.set_label(f"{len(updates)} update{'s' if len(updates) != 1 else ''} available")
        elif self.controller.checking_updates:
            title.set_label("Checking for updates…")
        elif never_checked:
            title.set_label("Updates have not been checked yet")
        elif self.controller.update_errors:
            title.set_label("Some sources could not be checked")
        else:
            title.set_label("Your software is up to date")
        inner.append(title)
        total = sum(u.download_size or 0 for u in updates)
        parts = []
        if updates:
            parts.append(f"{len(apps)} application(s), {len(updates) - len(apps)} other package(s)")
        if total:
            parts.append(f"about {size_text(total)} to download")
        if parts:
            sub = Gtk.Label(label=" · ".join(parts), xalign=0, wrap=True)
            sub.add_css_class("dim-label")
            inner.append(sub)
        checked = [f"{SOURCE_LABELS[s]} {relative_time(db.updates_checked_at(s))}" for s in enabled]
        info = Gtk.Label(label="Last checked: " + ", ".join(checked), xalign=0, wrap=True)
        info.add_css_class("caption")
        info.add_css_class("dim-label")
        inner.append(info)
        lists = apt_lists_age()
        if lists:
            age = Gtk.Label(label=f"APT package lists refreshed {relative_time(lists)}", xalign=0)
            age.add_css_class("caption")
            age.add_css_class("dim-label")
            inner.append(age)
        card.append(inner)
        batchable = [u for u in updates if u.source in BATCH_SOURCES]
        if batchable:
            update_all = Gtk.Button(label="Update All", valign=Gtk.Align.CENTER, margin_end=20)
            update_all.add_css_class("suggested-action")
            update_all.add_css_class("pill")
            update_all.set_tooltip_text("APT, Snap and Flatpak updates; GitHub releases are installed one by one")
            update_all.connect("clicked", lambda *_: self._request(
                [u for u in inv.updates.values() if u.source in BATCH_SOURCES], True))
            card.append(update_all)
        elif not never_checked and not self.controller.checking_updates and not self.controller.update_errors:
            image = Gtk.Image.new_from_icon_name("emblem-ok-symbolic")
            image.set_pixel_size(48)
            image.add_css_class("dim-label")
            image.set_margin_end(24)
            card.append(image)
        self.content.append(card)

        for source, error in self.controller.update_errors.items():
            box = Gtk.Box(spacing=10)
            box.add_css_class("danger-box")
            box.append(Gtk.Image.new_from_icon_name("dialog-error-symbolic"))
            box.append(Gtk.Label(label=f"{SOURCE_LABELS.get(source, source)}: {error}", xalign=0, wrap=True,
                                 hexpand=True))
            self.content.append(box)

        for source in UPDATE_SOURCES:
            group_updates = [u for u in updates if u.source == source]
            if not group_updates:
                continue
            app_updates = sorted([u for u in group_updates if u.is_application], key=lambda u: u.name.casefold())
            other = sorted([u for u in group_updates if not u.is_application], key=lambda u: u.name.casefold())
            group = Adw.PreferencesGroup(title=SOURCE_LABELS[source])
            if source == "github":
                group.set_description("New releases of apps installed from GitHub. Each one is downloaded first, "
                                      "then installed after its own preview.")
                for u in sorted(group_updates, key=lambda u: u.name.casefold()):
                    group.add(self._github_row(u))
                self.content.append(group)
                continue
            if source == "apt" and any(u.phased for u in group_updates):
                group.set_description("Some Ubuntu updates are phased: they roll out gradually and APT may hold "
                                      "them back for a few days.")
            batch = Gtk.Button(label=f"Update {SOURCE_LABELS[source]}", valign=Gtk.Align.CENTER)
            batch.add_css_class("flat")
            batch.connect("clicked", lambda _b, us=group_updates, s=source: self._request(us, s == "apt"))
            group.set_header_suffix(batch)
            for u in app_updates:
                group.add(self._row(u))
            if other:
                expander = Adw.ExpanderRow(title=esc("System packages and libraries" if source == "apt"
                                                     else "Runtimes and components"),
                                           subtitle=f"{len(other)} update{'s' if len(other) != 1 else ''}")
                expander.add_prefix(Gtk.Image.new_from_icon_name("package-x-generic-symbolic"))
                for u in other:
                    expander.add_row(self._row(u, compact=True))
                group.add(expander)
            self.content.append(group)

        if not updates:
            note = Gtk.Label(label="APT updates are read from the local package lists; use the refresh button "
                                   "in the header to download the newest lists first. Snap and Flatpak updates "
                                   "are checked against their online stores.", xalign=0, wrap=True)
            note.add_css_class("dim-label")
            self.content.append(note)

    def _github_row(self, u: UpdateInfo) -> Adw.ActionRow:
        subtitle = f"{u.current_version or '?'} → {u.new_version} · {u.origin}"
        if u.download_size:
            subtitle += f" · {size_text(u.download_size)}"
        row = Adw.ActionRow(title=esc(u.name), subtitle=esc(subtitle))
        record = next((self.controller.inventory.records.get(k) for k in u.app_keys
                       if k in self.controller.inventory.records), None)
        row.add_prefix(app_image(record, 32))
        notes = Gtk.Button(icon_name="adw-external-link-symbolic", valign=Gtk.Align.CENTER,
                           tooltip_text="Release notes on GitHub")
        notes.add_css_class("flat")
        url = u.extra.get("release_url", "")
        notes.connect("clicked", lambda *_: url.startswith("https://github.com/") and
                      Gtk.UriLauncher.new(url).launch(self.get_root(), None, None))
        row.add_suffix(notes)
        if u.extra.get("asset"):
            button = Gtk.Button(label="Download & Install" if u.extra.get("kind") == "deb" else "Download & Update",
                                valign=Gtk.Align.CENTER)
            button.connect("clicked", lambda *_: self.emit("github-update", u))
            row.add_suffix(button)
        else:
            row.add_suffix(pill("Download manually", "neutral"))
        return row

    def _row(self, u: UpdateInfo, compact: bool = False) -> Adw.ActionRow:
        subtitle = f"{u.current_version or '?'} → {u.new_version}"
        if u.download_size:
            subtitle += f" · {size_text(u.download_size)}"
        if u.origin:
            subtitle += f" · {u.origin}"
        row = Adw.ActionRow(title=esc(u.name), subtitle=esc(subtitle))
        check = Gtk.CheckButton(valign=Gtk.Align.CENTER, active=u.key in self.selected)
        check.connect("toggled", lambda c, k=u.key: self._toggle(k, c.get_active()))
        row.add_prefix(check)
        if not compact:
            record = next((self.controller.inventory.records.get(k) for k in u.app_keys
                           if k in self.controller.inventory.records), None)
            row.add_prefix(app_image(record, 32))
        if u.installation:
            row.add_suffix(pill(u.installation, "neutral"))
        if u.phased:
            row.add_suffix(pill("Phased", "warning"))
        button = Gtk.Button(label="Update", valign=Gtk.Align.CENTER)
        button.connect("clicked", lambda *_: self._request([u], False))
        row.add_suffix(button)
        return row
