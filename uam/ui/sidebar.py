"""Sidebar navigation."""

from __future__ import annotations

from gi.repository import GObject, Gtk

SECTIONS = [
    ("overview", "Overview", "view-grid-symbolic"),
    ("applications", "All Applications", "view-app-grid-symbolic"),
    ("updates", "Updates", "software-update-available-symbolic"),
    ("manual", "Manual Applications", "folder-open-symbolic"),
    ("managers", "Package Managers", "package-x-generic-symbolic"),
    ("history", "History", "document-open-recent-symbolic"),
    ("settings", "Settings", "emblem-system-symbolic"),
]


class _SectionRow(Gtk.ListBoxRow):
    def __init__(self, section_id: str, title: str, icon: str):
        super().__init__()
        self.section_id = section_id
        self.title = title
        box = Gtk.Box(spacing=12, margin_top=8, margin_bottom=8, margin_start=6, margin_end=6)
        box.append(Gtk.Image.new_from_icon_name(icon))
        label = Gtk.Label(label=title, xalign=0, hexpand=True, ellipsize=3)
        box.append(label)
        self.count = Gtk.Label(visible=False, valign=Gtk.Align.CENTER)
        self.count.add_css_class("sidebar-count")
        box.append(self.count)
        self.set_child(box)

    def set_count(self, value: int | None, accent: bool = False) -> None:
        if not value:
            self.count.set_visible(False)
            return
        self.count.set_label(str(value))
        self.count.set_visible(True)
        if accent:
            self.count.add_css_class("accent")
        else:
            self.count.remove_css_class("accent")


class Sidebar(Gtk.ListBox):
    __gtype_name__ = "UamSidebar"
    __gsignals__ = {"section-selected": (GObject.SignalFlags.RUN_FIRST, None, (str,))}

    def __init__(self):
        super().__init__(selection_mode=Gtk.SelectionMode.SINGLE)
        self.add_css_class("navigation-sidebar")
        self.rows: dict[str, _SectionRow] = {}
        for section_id, title, icon in SECTIONS:
            row = _SectionRow(section_id, title, icon)
            self.rows[section_id] = row
            self.append(row)
        self.connect("row-activated", self._on_activated)

    def _on_activated(self, _box, row: _SectionRow) -> None:
        self.emit("section-selected", row.section_id)

    def select_section(self, section_id: str) -> None:
        row = self.rows.get(section_id)
        if row is not None and self.get_selected_row() is not row:
            self.select_row(row)

    def title_for(self, section_id: str) -> str:
        return self.rows[section_id].title

    def set_count(self, section_id: str, value: int | None, accent: bool = False) -> None:
        self.rows[section_id].set_count(value, accent)
