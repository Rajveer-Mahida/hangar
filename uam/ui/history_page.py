"""Activity log."""

from __future__ import annotations

from gi.repository import Adw, Gtk

from ..services.inventory_service import AppController
from .widgets import LazyRefresh, clear_box, esc, format_date

FILTERS = [
    ("", "All activity"),
    ("install", "Installations"),
    ("remove", "Removals"),
    ("update", "Updates"),
    ("failed", "Failed operations"),
    ("discover", "Discoveries"),
    ("user", "User actions"),
]

ACTION_TEXT = {
    "install": ("Installed", "list-add-symbolic"),
    "remove": ("Removed", "user-trash-symbolic"),
    "update": ("Updated", "software-update-available-symbolic"),
    "refresh": ("Refreshed", "view-refresh-symbolic"),
    "cleanup": ("Cleaned up", "edit-clear-all-symbolic"),
    "discover": ("Discovered", "system-search-symbolic"),
    "classify": ("Reviewed", "emblem-ok-symbolic"),
    "settings": ("Changed setting", "emblem-system-symbolic"),
    "backup": ("Backed up", "document-save-symbolic"),
    "scan": ("Scanned", "system-search-symbolic"),
    "update-check": ("Checked updates", "view-refresh-symbolic"),
    "launch": ("Opened", "media-playback-start-symbolic"),
}
STATUS_ICONS = {"success": "emblem-ok-symbolic", "failed": "dialog-error-symbolic",
                "cancelled": "process-stop-symbolic", "info": "dialog-information-symbolic"}


class HistoryPage(Gtk.Box, LazyRefresh):
    __gtype_name__ = "UamHistoryPage"

    def __init__(self, controller: AppController, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.controller = controller
        self.window = window
        self.filter = ""

        self.dropdown = Gtk.DropDown.new_from_strings([label for _, label in FILTERS])
        self.dropdown.connect("notify::selected", self._on_filter)
        clear = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Clear history")
        clear.connect("clicked", self._on_clear)
        self.header_end = [clear, self.dropdown]

        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.list.add_css_class("boxed-list")
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=24, margin_bottom=32,
                               margin_start=18, margin_end=18)
        note = Gtk.Label(label="Operations, discoveries and your review decisions are recorded here. Command "
                               "output and credentials are never stored.", xalign=0, wrap=True)
        note.add_css_class("dim-label")
        self.content.append(note)
        self.content.append(self.list)
        self.empty = Adw.StatusPage(icon_name="document-open-recent-symbolic", title="No Activity Yet")
        self.empty.add_css_class("compact")
        self.content.append(self.empty)
        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        sw.set_child(Adw.Clamp(maximum_size=900, child=self.content))
        self.append(sw)
        self.init_lazy_refresh()
        controller.connect("history-changed", self.queue_refresh)
        self.refresh()

    def _on_filter(self, dropdown, _pspec) -> None:
        self.filter = FILTERS[dropdown.get_selected()][0]
        self.refresh()

    def _entries(self):
        db = self.controller.db
        f = self.filter
        if f in ("", "failed", "user"):
            entries = db.history(limit=1000)
            if f == "failed":
                entries = [e for e in entries if e["status"] == "failed"]
            elif f == "user":
                entries = [e for e in entries if e["action"] in ("classify", "settings", "backup", "launch")]
            return entries[:500]
        return db.history(limit=500, action=f)

    def refresh(self) -> None:
        clear_box(self.list)
        entries = self._entries()
        self.list.set_visible(bool(entries))
        self.empty.set_visible(not entries)
        for e in entries:
            verb, icon = ACTION_TEXT.get(e["action"], (e["action"].capitalize(), "dialog-information-symbolic"))
            title = f"{verb} {e['target']}"
            if e["status"] == "failed":
                title = f"Failed: {e['action']} {e['target']}"
            elif e["status"] == "cancelled":
                title = f"Cancelled: {e['action']} {e['target']}"
            parts = [format_date(e["ts"], with_time=True)]
            if e["source"]:
                parts.append(e["source"])
            if e["detail"] and e["detail"] != e["target"] and not e["detail"].startswith(("Uninstalling", "Updating")):
                parts.append(e["detail"])
            row = Adw.ActionRow(title=esc(title), subtitle=esc(" · ".join(parts)), subtitle_lines=3)
            row.add_prefix(Gtk.Image.new_from_icon_name(icon))
            status = Gtk.Image.new_from_icon_name(STATUS_ICONS.get(e["status"], "dialog-information-symbolic"))
            if e["status"] == "failed":
                status.add_css_class("error")
            elif e["status"] == "success":
                status.add_css_class("success")
            row.add_suffix(status)
            self.list.append(row)

    def _on_clear(self, _button) -> None:
        dialog = Adw.AlertDialog(heading="Clear History?",
                                 body="All recorded activity will be deleted. This cannot be undone.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("clear", "Clear History")
        dialog.set_response_appearance("clear", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def on_response(_d, response):
            if response == "clear":
                self.controller.db.clear_history()
                self.refresh()

        dialog.connect("response", on_response)
        dialog.present(self.window)

