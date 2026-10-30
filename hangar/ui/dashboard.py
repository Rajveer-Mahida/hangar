"""Overview dashboard."""

from __future__ import annotations

from gi.repository import Adw, Gdk, GObject, Graphene, Gsk, Gtk

from ..core.application_model import SOURCE_PRIORITY, AppRecord, Source
from ..services.inventory_service import AppController
from ..services.privilege_service import helper_status
from .widgets import (
    SOURCE_ICONS,
    app_image,
    LazyRefresh,
    clear_box,
    css_source,
    esc,
    relative_time,
    scrolled,
    section_label,
    source_badge,
)

SOURCE_COLORS = {
    Source.APT: "#e66100",
    Source.VENDOR: "#9141ac",
    Source.SNAP: "#26a269",
    Source.FLATPAK: "#3584e4",
    Source.APPIMAGE: "#2190a4",
    Source.MANUAL: "#c88800",
    Source.UNKNOWN: "#9a9996",
}


class CompositionBar(Gtk.Widget):
    """Horizontal stacked bar showing the share of each installation source."""

    __gtype_name__ = "HangarCompositionBar"

    def __init__(self):
        super().__init__()
        self.values: list[tuple[Source, int]] = []
        self.set_size_request(-1, 14)
        self.set_hexpand(True)

    def set_values(self, values: list[tuple[Source, int]]) -> None:
        self.values = [(s, v) for s, v in values if v > 0]
        self.queue_draw()

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        width, height = self.get_width(), self.get_height()
        total = sum(v for _, v in self.values)
        rect = Graphene.Rect().init(0, 0, width, height)
        rounded = Gsk.RoundedRect()
        rounded.init_from_rect(rect, height / 2)
        snapshot.push_rounded_clip(rounded)
        if total == 0:
            color = Gdk.RGBA()
            color.parse("rgba(127,127,127,0.15)")
            snapshot.append_color(color, rect)
        else:
            x = 0.0
            gap = 2.0
            for i, (source, value) in enumerate(self.values):
                w = width * value / total
                color = Gdk.RGBA()
                color.parse(SOURCE_COLORS[source])
                seg_w = max(0.0, w - (gap if i < len(self.values) - 1 else 0))
                snapshot.append_color(color, Graphene.Rect().init(x, 0, seg_w, height))
                x += w
        snapshot.pop()


class StatCard(Gtk.Button):
    def __init__(self, title: str, icon: str, css: str):
        super().__init__()
        self.add_css_class("card")
        self.add_css_class("stat-card")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        top = Gtk.Box()
        image = Gtk.Image.new_from_icon_name(icon)
        image.add_css_class("stat-icon")
        image.add_css_class(css)
        image.set_halign(Gtk.Align.START)
        top.append(image)
        box.append(top)
        self.number = Gtk.Label(label="–", xalign=0)
        self.number.add_css_class("stat-number")
        box.append(self.number)
        label = Gtk.Label(label=title, xalign=0, ellipsize=3)
        label.add_css_class("stat-label")
        box.append(label)
        self.set_child(box)

    def set_value(self, value: int) -> None:
        self.number.set_label(f"{value:,}")


class DashboardPage(Gtk.Box, LazyRefresh):
    __gtype_name__ = "HangarDashboardPage"
    __gsignals__ = {
        "navigate": (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
        "show-app": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, controller: AppController):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.controller = controller
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=26, margin_top=24, margin_bottom=32,
                          margin_start=18, margin_end=18)

        # Hero ---------------------------------------------------------------
        hero = Gtk.Box(spacing=12)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        title = Gtk.Label(label="Software Inventory", xalign=0, wrap=True)
        title.add_css_class("hero-title")
        titles.append(title)
        self.subtitle = Gtk.Label(label="Scanning your system…", xalign=0, wrap=True)
        self.subtitle.add_css_class("dim-label")
        titles.append(self.subtitle)
        hero.append(titles)
        self.rescan_button = Gtk.Button(valign=Gtk.Align.CENTER)
        self.rescan_content = Adw.ButtonContent(icon_name="view-refresh-symbolic", label="Rescan")
        self.rescan_button.set_child(self.rescan_content)
        self.rescan_button.set_tooltip_text("Rescan the system (Ctrl+R)")
        self.rescan_button.add_css_class("pill")
        self.rescan_button.connect("clicked", lambda *_: controller.rescan("user"))
        hero.append(self.rescan_button)
        install = Gtk.Button(valign=Gtk.Align.CENTER, action_name="win.install", tooltip_text="Install software")
        self.install_content = Adw.ButtonContent(icon_name="list-add-symbolic", label="Install Software")
        install.set_child(self.install_content)
        install.add_css_class("pill")
        install.add_css_class("suggested-action")
        hero.append(install)
        content.append(hero)

        # Stat cards -----------------------------------------------------------
        self.cards: dict[str, StatCard] = {}
        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True, column_spacing=12,
                           row_spacing=12, min_children_per_line=2, max_children_per_line=4)
        specs = [
            ("total", "Total applications", "view-app-grid-symbolic", "total", "applications", ""),
            ("updates", "Available updates", "software-update-available-symbolic", "updates", "updates", ""),
            ("apt", "APT", SOURCE_ICONS[Source.APT], css_source(Source.APT), "applications", "apt"),
            ("vendor", "Vendor packages", SOURCE_ICONS[Source.VENDOR], css_source(Source.VENDOR), "applications",
             "vendor"),
            ("snap", "Snap", SOURCE_ICONS[Source.SNAP], css_source(Source.SNAP), "applications", "snap"),
            ("flatpak", "Flatpak", SOURCE_ICONS[Source.FLATPAK], css_source(Source.FLATPAK), "applications",
             "flatpak"),
            ("appimage", "AppImage", SOURCE_ICONS[Source.APPIMAGE], css_source(Source.APPIMAGE), "applications",
             "appimage"),
            ("manual_all", "Manually installed", SOURCE_ICONS[Source.MANUAL], css_source(Source.MANUAL), "manual",
             ""),
        ]
        for key, label, icon, css, section, arg in specs:
            card = StatCard(label, icon, css)
            card.connect("clicked", lambda _b, s=section, a=arg: self.emit("navigate", s, a))
            self.cards[key] = card
            flow.append(card)
        content.append(flow)

        # Composition -----------------------------------------------------------
        comp = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        comp.add_css_class("card")
        comp_inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=16, margin_bottom=16,
                             margin_start=18, margin_end=18)
        comp_inner.append(section_label("Inventory by source"))
        self.bar = CompositionBar()
        comp_inner.append(self.bar)
        self.legend = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, column_spacing=18, row_spacing=6,
                                  min_children_per_line=2, max_children_per_line=7)
        comp_inner.append(self.legend)
        comp.append(comp_inner)
        content.append(comp)

        # Recent + attention ---------------------------------------------------
        columns = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True, column_spacing=18,
                              row_spacing=18, min_children_per_line=1, max_children_per_line=2)
        recent_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, valign=Gtk.Align.START)
        recent_box.append(section_label("Recently installed"))
        self.recent_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.recent_list.add_css_class("boxed-list")
        self.recent_list.connect("row-activated", self._on_recent_activated)
        recent_box.append(self.recent_list)
        columns.append(recent_box)

        attention_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, valign=Gtk.Align.START)
        attention_box.append(section_label("Needs attention"))
        self.attention_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.attention_list.add_css_class("boxed-list")
        attention_box.append(self.attention_list)
        columns.append(attention_box)
        content.append(columns)

        self.append(scrolled(content))
        self.init_lazy_refresh()
        controller.connect("inventory-changed", self.queue_refresh)
        controller.connect("updates-changed", self.queue_refresh)
        controller.connect("scan-state", self._on_scan_state)
        self.refresh()

    # -----------------------------------------------------------------------
    def _on_scan_state(self, _c, running: bool, message: str, _fraction: float) -> None:
        self.rescan_button.set_sensitive(not running)
        if running:
            self.subtitle.set_label(f"{message}…")
        else:
            self._update_subtitle()

    def _update_subtitle(self) -> None:
        inv = self.controller.inventory
        if inv.last_scan:
            text = f"Last scanned {relative_time(inv.last_scan)}"
            if inv.scan_errors:
                text += f" · {len(inv.scan_errors)} warning(s)"
            self.subtitle.set_label(text)
        elif not self.controller.scanning:
            self.subtitle.set_label("No scan yet")

    def refresh(self) -> None:
        inv = self.controller.inventory
        counts = inv.counts()
        for key, card in self.cards.items():
            card.set_value(counts.get(key, 0))
        if not self.controller.scanning:
            self._update_subtitle()

        listed = inv.listed_records(group=False)
        by_source: dict[Source, int] = {s: 0 for s in Source}
        for r in listed:
            by_source[r.source] += 1
        ordered = [(s, by_source[s]) for s in SOURCE_PRIORITY]
        self.bar.set_values(ordered)
        clear_box(self.legend)
        total = max(1, sum(by_source.values()))
        for source, value in ordered:
            if not value:
                continue
            item = Gtk.Box(spacing=8)
            dot = Gtk.Box(valign=Gtk.Align.CENTER)
            dot.add_css_class("legend-dot")
            dot.add_css_class(f"legend-{source.value}")
            item.append(dot)
            label = Gtk.Label(label=f"{source.label}")
            item.append(label)
            pct = Gtk.Label(label=f"{value} · {round(100 * value / total)}%")
            pct.add_css_class("dim-label")
            item.append(pct)
            self.legend.append(item)

        clear_box(self.recent_list)
        recent = inv.recent(6)
        if not recent:
            row = Adw.ActionRow(title="Nothing yet", subtitle="Installation dates appear after the first scan")
            self.recent_list.append(row)
        for record in recent:
            self.recent_list.append(self._recent_row(record))

        clear_box(self.attention_list)
        items = self._attention_items(counts)
        if not items:
            row = Adw.ActionRow(title="All good", subtitle="No updates, reviews or broken launchers")
            row.add_prefix(Gtk.Image.new_from_icon_name("emblem-ok-symbolic"))
            self.attention_list.append(row)
        for icon, title, subtitle, section in items:
            row = Adw.ActionRow(title=esc(title), subtitle=esc(subtitle), activatable=True)
            row.add_prefix(Gtk.Image.new_from_icon_name(icon))
            row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            row.connect("activated", lambda _r, s=section: self.emit("navigate", s, ""))
            self.attention_list.append(row)

    def _attention_items(self, counts) -> list[tuple[str, str, str, str]]:
        inv = self.controller.inventory
        items = []
        if counts["app_updates"] or counts["updates"]:
            other = counts["updates"] - counts["app_updates"]
            subtitle = f"{counts['app_updates']} application(s)"
            if other:
                subtitle += f", {other} system package(s)"
            items.append(("software-update-available-symbolic", f"{counts['updates']} updates available",
                          subtitle, "updates"))
        broken = sum(1 for r in inv.records.values()
                     if (r.extra.get("broken_launcher") or r.extra.get("broken_link")) and r.classification is None)
        if broken:
            items.append(("dialog-warning-symbolic", f"{broken} broken launcher(s) or link(s)",
                          "Left over from software that was removed", "manual"))
        review = counts["needs_review"] - broken
        if review > 0:
            items.append(("dialog-question-symbolic", f"{review} item(s) need review",
                          "Found by scanning but not identified with certainty", "manual"))
        if inv.scan_errors:
            items.append(("dialog-error-symbolic", "Some sources could not be scanned",
                          "; ".join(inv.scan_errors)[:120], "managers"))
        if self.controller.update_errors:
            items.append(("network-error-symbolic", "Update check incomplete",
                          "; ".join(f"{k}: {v}" for k, v in self.controller.update_errors.items())[:120], "updates"))
        if not helper_status()["installed"]:
            items.append(("dialog-password-symbolic", "Privileged helper not installed",
                          "Running in development mode; see Package Managers", "managers"))
        return items

    def _recent_row(self, record: AppRecord) -> Adw.ActionRow:
        row = Adw.ActionRow(title=esc(record.name), activatable=True)
        row.set_subtitle(relative_time(record.effective_date))
        row.add_prefix(app_image(record, 32))
        row.add_suffix(source_badge(record.source))
        row.app_key = record.key
        return row

    def _on_recent_activated(self, _list, row) -> None:
        key = getattr(row, "app_key", None)
        if key:
            self.emit("show-app", key)

