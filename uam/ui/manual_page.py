"""Manually installed software and the review queue of the discovery engine."""

from __future__ import annotations

from gi.repository import Adw, Gio, GLib, GObject, Gtk

from ..core.application_model import AppRecord, Classification, Confidence, Source
from ..services.inventory_service import AppController
from .applications_page import build_app_menu, lazy_menu
from .widgets import LazyRefresh, app_image, clear_box, confidence_pill, esc, pill, source_badge

FILTERS = [
    ("all", "All"),
    ("review", "Needs review"),
    ("broken", "Broken leftovers"),
    ("detected", "Detected"),
    ("reviewed", "Reviewed"),
]


class ManualPage(Gtk.Box, LazyRefresh):
    __gtype_name__ = "UamManualPage"
    __gsignals__ = {
        "show-app": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "open-settings": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, controller: AppController):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.controller = controller
        self.filter = "all"

        folders = Gtk.Button(icon_name="folder-new-symbolic", tooltip_text="Configure scanned folders")
        folders.connect("clicked", lambda *_: self.emit("open-settings"))
        self.header_end = [folders]

        bar = Gtk.Box(spacing=6)
        bar.add_css_class("filter-bar")
        box = Gtk.Box()
        box.add_css_class("linked")
        first = None
        self.filter_buttons: dict[str, Gtk.ToggleButton] = {}
        for key, label in FILTERS:
            b = Gtk.ToggleButton(label=label, group=first)
            first = first or b
            b.connect("toggled", self._on_filter, key)
            self.filter_buttons[key] = b
            box.append(b)
        bar.append(box)
        scroller = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True,
                                      propagate_natural_width=True)
        scroller.set_child(bar)
        # Same clamp and side margins as the content column below, so they line up
        # (18px content margin minus the bar's own 6px padding).
        scroller.set_margin_start(12)
        scroller.set_margin_end(12)
        self.top_bar = Adw.Clamp(maximum_size=980, child=scroller)

        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20, margin_top=14, margin_bottom=32,
                               margin_start=18, margin_end=18)
        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        sw.set_child(Adw.Clamp(maximum_size=980, child=self.content))
        self.append(sw)
        first.set_active(True)  # content exists now, so the filter handler can refresh
        # Rebuilt from an idle callback (LazyRefresh), so a menu action never
        # destroys its own row while the signal is still being emitted.
        self.init_lazy_refresh()
        controller.connect("inventory-changed", self.queue_refresh)
        self.refresh()

    def _on_filter(self, button: Gtk.ToggleButton, key: str) -> None:
        if button.get_active():
            self.filter = key
            self.refresh()

    def show_filter(self, key: str) -> None:
        if key in self.filter_buttons:
            self.filter_buttons[key].set_active(True)

    def _matches(self, r: AppRecord) -> bool:
        broken = bool(r.extra.get("broken_launcher") or r.extra.get("broken_link"))
        if self.filter == "review":
            return self.controller.inventory.needs_review(r) and not broken
        if self.filter == "broken":
            return broken and r.classification != Classification.IGNORE
        if self.filter == "detected":
            return r.confidence == Confidence.DETECTED
        if self.filter == "reviewed":
            return r.classification is not None
        return True

    def refresh(self) -> None:
        clear_box(self.content)
        inv = self.controller.inventory
        records = [r for r in inv.manual_records() if self._matches(r)]
        records.sort(key=lambda r: (r.classification == Classification.IGNORE, r.confidence != Confidence.UNKNOWN,
                                    r.name.casefold()))

        intro = Gtk.Box(spacing=12)
        intro.add_css_class("info-box")
        intro.append(Gtk.Image.new_from_icon_name("system-search-symbolic"))
        text = ("The discovery engine looks for software no package manager knows about: unowned folders in /opt, "
                "programs in /usr/local/bin, AppImages, launchers in ~/.local/share/applications and the folders "
                "you add in Settings. Items it cannot identify with certainty need your review — mark them as an "
                "Application, a Component or Ignore them. Nothing is ever deleted by scanning.")
        intro.append(Gtk.Label(label=text, wrap=True, xalign=0, hexpand=True))
        self.content.append(intro)

        dirs = list(self.controller.settings.custom_dirs)
        if dirs:
            label = Gtk.Label(label="Additional scanned folders: " + ", ".join(dirs), xalign=0, wrap=True)
            label.add_css_class("dim-label")
            label.add_css_class("caption")
            self.content.append(label)

        if not records:
            status = Adw.StatusPage(icon_name="emblem-ok-symbolic", title="Nothing Here",
                                    description="No manually installed items match this filter.")
            status.add_css_class("compact")
            self.content.append(status)
            return

        groups = [
            ("Applications", [r for r in records if r.confidence != Confidence.UNKNOWN
                              and r.classification not in (Classification.IGNORE, Classification.COMPONENT)
                              and not r.extra.get("broken_launcher")]),
            ("Needs Review", [r for r in records if r.confidence == Confidence.UNKNOWN and r.classification is None]),
            ("Reviewed", [r for r in records if r.classification is not None and not (
                r.confidence != Confidence.UNKNOWN and r.classification == Classification.APPLICATION)]),
        ]
        for title, items in groups:
            if not items:
                continue
            group = Adw.PreferencesGroup(title=f"{title} ({len(items)})")
            for r in items:
                group.add(self._row(r))
            self.content.append(group)

    def _row(self, r: AppRecord) -> Adw.ActionRow:
        subtitle = r.install_path or r.desktop_file
        if r.notes and (r.extra.get("broken_launcher") or r.extra.get("broken_link")):
            subtitle = r.notes[0]
        row = Adw.ActionRow(title=esc(r.name), subtitle=esc(subtitle), activatable=True, subtitle_lines=2)
        row.add_prefix(app_image(r, 36))
        row.connect("activated", lambda *_: self.emit("show-app", r.key))
        badges = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        if r.extra.get("broken_launcher") or r.extra.get("broken_link"):
            badges.append(pill("Broken", "error"))
        elif r.extra.get("web_app"):
            badges.append(pill("Web app", "neutral"))
        elif r.source != Source.MANUAL:
            badges.append(source_badge(r.source))
        if r.confidence == Confidence.UNKNOWN:
            badges.append(confidence_pill(r.confidence))
        row.add_suffix(badges)

        label = r.classification.value.capitalize() if r.classification else "Review"
        review = Gtk.MenuButton(label=label, valign=Gtk.Align.CENTER, tooltip_text="Tell the scanner what this item is")
        lazy_menu(review, lambda r=r: self._review_menu(r))
        if r.classification is None and r.confidence == Confidence.UNKNOWN:
            review.add_css_class("suggested-action")
        row.add_suffix(review)

        menu = Gtk.MenuButton(icon_name="view-more-symbolic", valign=Gtk.Align.CENTER)
        menu.add_css_class("flat")
        lazy_menu(menu, lambda r=r: build_app_menu(r, None))
        row.add_suffix(menu)
        return row

    @staticmethod
    def _review_menu(r: AppRecord) -> Gio.Menu:
        menu = Gio.Menu()
        for kind, label in ((Classification.APPLICATION, "Application"), (Classification.COMPONENT, "Component"),
                            (Classification.IGNORE, "Ignore")):
            item = Gio.MenuItem.new(("✓ " if r.classification == kind else "") + f"Mark as {label}", None)
            item.set_action_and_target_value("win.app-classify", GLib.Variant.new_string(f"{kind.value}|{r.key}"))
            menu.append_item(item)
        if r.classification is not None:
            item = Gio.MenuItem.new("Clear Review", None)
            item.set_action_and_target_value("win.app-classify", GLib.Variant.new_string(f"none|{r.key}"))
            menu.append_item(item)
        return menu

