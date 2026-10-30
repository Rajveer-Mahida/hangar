"""All Applications: search, filters, sorting, list/grid views and multi-selection."""

from __future__ import annotations

from gi.repository import Adw, Gio, GLib, GObject, Gtk

from ..core.application_model import SOURCE_PRIORITY, AppRecord, PackageManager, Source, UpdateInfo
from ..services.inventory_service import AppController
from .widgets import (
    LazyRefresh,
    app_image,
    format_date,
    gicon_for,
    pill,
    set_source_badge,
    source_badge,
)

SOURCE_FILTERS = [("", "All sources")] + [(s.value, s.label) for s in Source]
SORT_MODES = [("name", "Name"), ("date", "Installation date"), ("source", "Package source"), ("size", "Size")]


class AppItem(GObject.Object):
    __gtype_name__ = "UamAppItem"

    def __init__(self, record: AppRecord, update: UpdateInfo | None, installations: int):
        super().__init__()
        self.record = record
        self.update = update
        self.installations = installations
        self.search_text = " ".join([
            record.name, record.summary, record.package_name, record.source.label, record.origin,
            " ".join(record.categories), record.category,
        ]).casefold()


def lazy_menu(button: Gtk.MenuButton, build) -> None:
    """Create the menu only when the button is clicked.

    A menu model set up front makes GTK build a GtkPopoverMenu immediately,
    which is expensive when many rows are created or recycled.
    """
    button.set_menu_model(None)
    button.set_create_popup_func(lambda b: b.set_menu_model(build()))


def build_app_menu(record: AppRecord, update: UpdateInfo | None) -> Gio.Menu:
    menu = Gio.Menu()
    key = GLib.Variant.new_string(record.key)

    def item(label: str, action: str) -> Gio.MenuItem:
        mi = Gio.MenuItem.new(label, None)
        mi.set_action_and_target_value(f"win.{action}", key)
        return mi

    main = Gio.Menu()
    if record.has_launcher:
        main.append_item(item("Open", "app-launch"))
    main.append_item(item("View Details", "app-details"))
    main.append_item(item("Locate Installation", "app-locate"))
    main.append_item(item("Copy Package Information", "app-copy"))
    menu.append_section(None, main)
    if record.package_manager != PackageManager.NONE:
        upd = Gio.Menu()
        upd.append_item(item("Check for Updates", "app-check-update"))
        if update:
            upd.append_item(item(f"Update to {update.new_version}", "app-update"))
        menu.append_section(None, upd)
    danger = Gio.Menu()
    danger.append_item(item("Uninstall…", "app-uninstall"))
    menu.append_section(None, danger)
    return menu


class _ListRow(Gtk.Box):
    def __init__(self, page: "ApplicationsPage"):
        super().__init__(spacing=12)
        self.page = page
        self.key = ""
        self.add_css_class("app-row")
        self.check = Gtk.CheckButton(valign=Gtk.Align.CENTER)
        self.check.add_css_class("selection-mode")
        self._check_handler = self.check.connect("toggled", self._on_toggled)
        self.append(self.check)
        self.image = app_image(None, 40)
        self.append(self.image)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True, valign=Gtk.Align.CENTER)
        title_row = Gtk.Box(spacing=8)
        self.title = Gtk.Label(xalign=0, ellipsize=3)
        self.title.add_css_class("app-title")
        title_row.append(self.title)
        self.badge = source_badge(Source.APT)
        title_row.append(self.badge)
        self.extra = Gtk.Box(spacing=6)
        title_row.append(self.extra)
        text.append(title_row)
        self.subtitle = Gtk.Label(xalign=0, ellipsize=3)
        self.subtitle.add_css_class("app-subtitle")
        text.append(self.subtitle)
        self.append(text)

        meta = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER)
        self.version = Gtk.Label(xalign=1, ellipsize=3, max_width_chars=18)
        self.version.add_css_class("app-meta")
        meta.append(self.version)
        self.date = Gtk.Label(xalign=1)
        self.date.add_css_class("app-meta")
        meta.append(self.date)
        self.append(meta)

        self.menu = Gtk.MenuButton(icon_name="view-more-symbolic", valign=Gtk.Align.CENTER)
        self.menu.add_css_class("flat")
        self.menu.set_tooltip_text("Actions")
        self.append(self.menu)

    def bind(self, item: AppItem) -> None:
        r = item.record
        self.key = r.key
        self.image.set_from_gicon(gicon_for(r.icon))
        self.title.set_label(r.name)
        set_source_badge(self.badge, r.source)
        child = self.extra.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.extra.remove(child)
            child = nxt
        if item.update:
            self.extra.append(pill("Update", "update"))
        if item.installations > 1:
            self.extra.append(pill(f"{item.installations} installs", "neutral"))
        if r.hidden:
            self.extra.append(pill("Hidden", "neutral"))
        if r.is_component:
            self.extra.append(pill("Component", "neutral"))
        self.subtitle.set_label(r.summary or r.install_path)
        version = r.version
        if item.update:
            version = f"{r.version} → {item.update.new_version}"
        self.version.set_label(version or "")
        self.version.set_tooltip_text(version or None)
        self.date.set_label(format_date(r.effective_date))
        lazy_menu(self.menu, lambda r=r, u=item.update: build_app_menu(r, u))
        with self.check.handler_block(self._check_handler):
            self.check.set_active(r.key in self.page.selected_keys)
        self.check.set_visible(self.page.selection_mode)

    def _on_toggled(self, check: Gtk.CheckButton) -> None:
        self.page.set_selected(self.key, check.get_active())


class _GridTile(Gtk.Overlay):
    def __init__(self, page: "ApplicationsPage"):
        super().__init__()
        self.page = page
        self.key = ""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.add_css_class("app-tile")
        self.image = app_image(None, 64)
        box.append(self.image)
        self.title = Gtk.Label(justify=Gtk.Justification.CENTER, wrap=True, lines=2, ellipsize=3,
                               max_width_chars=16, width_chars=12)
        self.title.add_css_class("app-tile-name")
        box.append(self.title)
        badges = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
        self.badge = source_badge(Source.APT)
        badges.append(self.badge)
        self.update = pill("Update", "update")
        badges.append(self.update)
        box.append(badges)
        self.set_child(box)
        self.check = Gtk.CheckButton(halign=Gtk.Align.START, valign=Gtk.Align.START, margin_start=6, margin_top=6)
        self.check.add_css_class("selection-mode")
        self._handler = self.check.connect("toggled", lambda c: self.page.set_selected(self.key, c.get_active()))
        self.add_overlay(self.check)

    def bind(self, item: AppItem) -> None:
        r = item.record
        self.key = r.key
        self.image.set_from_gicon(gicon_for(r.icon))
        self.title.set_label(r.name)
        self.set_tooltip_text(r.summary or r.name)
        set_source_badge(self.badge, r.source)
        self.update.set_visible(item.update is not None)
        with self.check.handler_block(self._handler):
            self.check.set_active(r.key in self.page.selected_keys)
        self.check.set_visible(self.page.selection_mode)


class ApplicationsPage(Gtk.Box, LazyRefresh):
    __gtype_name__ = "UamApplicationsPage"
    __gsignals__ = {
        "show-app": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "uninstall-apps": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "update-apps": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self, controller: AppController):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.controller = controller
        self.selected_keys: set[str] = set()
        self.selection_mode = False
        self.source_filter = ""
        self.only_updates = False
        self.only_manual = False
        self.query_terms: list[str] = []
        self.sort_mode = "name"

        # Header widgets (placed by the window) --------------------------------
        self.search = Gtk.SearchEntry(placeholder_text="Search applications", hexpand=True)
        self.search.connect("search-changed", self._on_search)
        self.header_title = Adw.Clamp(maximum_size=420, child=self.search)

        self.view_toggle = Gtk.Box()
        self.view_toggle.add_css_class("linked")
        self.list_button = Gtk.ToggleButton(icon_name="view-list-symbolic", tooltip_text="List view")
        self.grid_button = Gtk.ToggleButton(icon_name="view-grid-symbolic", tooltip_text="Grid view",
                                            group=self.list_button)
        self.view_toggle.append(self.list_button)
        self.view_toggle.append(self.grid_button)
        self.select_button = Gtk.ToggleButton(icon_name="selection-mode-symbolic", tooltip_text="Select applications")
        self.select_button.connect("toggled", self._on_select_mode)
        self.header_end = [self.select_button, self.view_toggle]
        install = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Install software (Ctrl+I)",
                             action_name="win.install")
        self.header_start = [install]

        # Filter bar ------------------------------------------------------------
        bar = Gtk.Box(spacing=8)
        bar.add_css_class("filter-bar")
        self.source_dropdown = Gtk.DropDown.new_from_strings([label for _, label in SOURCE_FILTERS])
        self.source_dropdown.set_tooltip_text("Filter by installation source")
        self.source_dropdown.connect("notify::selected", self._on_source_changed)
        bar.append(self.source_dropdown)
        self.updates_toggle = Gtk.ToggleButton(tooltip_text="Only applications with an update")
        self.updates_content = Adw.ButtonContent(icon_name="software-update-available-symbolic", label="Has update")
        self.updates_toggle.set_child(self.updates_content)
        self.updates_toggle.connect("toggled", self._on_toggle_filter)
        bar.append(self.updates_toggle)
        self.manual_toggle = Gtk.ToggleButton(tooltip_text="Only manually installed applications")
        self.manual_content = Adw.ButtonContent(icon_name="folder-symbolic", label="Manually installed")
        self.manual_toggle.set_child(self.manual_content)
        self.manual_toggle.connect("toggled", self._on_toggle_filter)
        bar.append(self.manual_toggle)
        spacer = Gtk.Box(hexpand=True)
        bar.append(spacer)
        self.count_label = Gtk.Label()
        self.count_label.add_css_class("dim-label")
        bar.append(self.count_label)
        self.sort_label = Gtk.Label(label="Sort")
        self.sort_label.add_css_class("dim-label")
        bar.append(self.sort_label)
        self.sort_dropdown = Gtk.DropDown.new_from_strings([label for _, label in SORT_MODES])
        self.sort_dropdown.connect("notify::selected", self._on_sort_changed)
        bar.append(self.sort_dropdown)
        self.top_bar = Adw.Clamp(maximum_size=1100, tightening_threshold=900, child=bar)

        # Models ----------------------------------------------------------------
        self.store = Gio.ListStore(item_type=AppItem)
        self.filter = Gtk.CustomFilter.new(self._match)
        self.filter_model = Gtk.FilterListModel(model=self.store, filter=self.filter)
        self.sorter = Gtk.CustomSorter.new(self._compare)
        self.sort_model = Gtk.SortListModel(model=self.filter_model, sorter=self.sorter)
        self.selection = Gtk.NoSelection(model=self.sort_model)
        self.filter_model.connect("items-changed", lambda *_: self._update_count())

        list_factory = Gtk.SignalListItemFactory()
        list_factory.connect("setup", lambda _f, li: li.set_child(_ListRow(self)))
        list_factory.connect("bind", lambda _f, li: li.get_child().bind(li.get_item()))
        self.list_view = Gtk.ListView(model=self.selection, factory=list_factory, single_click_activate=True)
        self.list_view.add_css_class("app-list")
        self.list_view.connect("activate", self._on_activate)

        grid_factory = Gtk.SignalListItemFactory()
        grid_factory.connect("setup", lambda _f, li: li.set_child(_GridTile(self)))
        grid_factory.connect("bind", lambda _f, li: li.get_child().bind(li.get_item()))
        self.grid_view = Gtk.GridView(model=self.selection, factory=grid_factory, single_click_activate=True,
                                      min_columns=2, max_columns=8)
        self.grid_view.add_css_class("app-grid")
        self.grid_view.connect("activate", self._on_activate)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True)
        list_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        list_scroll.set_child(Adw.ClampScrollable(maximum_size=1100, tightening_threshold=900,
                                                  child=self.list_view))
        self.stack.add_named(list_scroll, "list")
        grid_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        grid_scroll.set_child(self.grid_view)
        self.stack.add_named(grid_scroll, "grid")
        self.empty = Adw.StatusPage(icon_name="edit-find-symbolic", title="No Applications Found",
                                    description="Try a different search or filter")
        self.stack.add_named(self.empty, "empty")
        self.append(self.stack)

        # Selection action bar ------------------------------------------------------
        self.action_bar = Gtk.ActionBar(revealed=False)
        self.selected_label = Gtk.Label(label="0 selected")
        self.action_bar.pack_start(self.selected_label)
        select_all = Gtk.Button(icon_name="edit-select-all-symbolic", tooltip_text="Select all shown applications")
        select_all.connect("clicked", lambda *_: self.select_all())
        self.action_bar.pack_start(select_all)
        clear = Gtk.Button(icon_name="edit-clear-all-symbolic", tooltip_text="Clear the selection")
        clear.connect("clicked", lambda *_: self.clear_selection())
        self.action_bar.pack_start(clear)
        self.uninstall_selected = Gtk.Button(label="Uninstall…")
        self.uninstall_selected.add_css_class("destructive-action")
        self.uninstall_selected.connect("clicked", lambda *_: self.emit("uninstall-apps", self._selected_records()))
        self.action_bar.pack_end(self.uninstall_selected)
        self.update_selected = Gtk.Button(label="Update")
        self.update_selected.add_css_class("suggested-action")
        self.update_selected.connect("clicked", lambda *_: self.emit("update-apps", self._selected_records()))
        self.action_bar.pack_end(self.update_selected)
        self.bottom_bar = self.action_bar

        view_mode = controller.settings.view_mode
        (self.grid_button if view_mode == "grid" else self.list_button).set_active(True)
        self.list_button.connect("toggled", self._on_view_toggled)
        self.init_lazy_refresh()
        controller.connect("inventory-changed", self.queue_refresh)
        controller.connect("updates-changed", self.queue_refresh)
        self.refresh()

    # -- data -------------------------------------------------------------------------
    def refresh(self) -> None:
        inv = self.controller.inventory
        updates = inv.updates_for_records()
        # When filtering by source, show the matching installation itself rather than
        # the group's preferred one.
        group = self.controller.settings.group_duplicates and not self.source_filter
        records = inv.listed_records(group=group)
        items = []
        for r in records:
            installs = len(inv.installations_of(r)) if group else 1
            items.append(AppItem(r, updates.get(r.key), installs))
        self.store.splice(0, self.store.get_n_items(), items)
        present = {r.key for r in records}
        self.selected_keys &= present
        self._update_count()
        self._update_selection_ui()

    def _update_count(self) -> None:
        n = self.filter_model.get_n_items()
        total = self.store.get_n_items()
        self.count_label.set_label(f"{n} of {total}" if n != total else f"{n} applications")
        if n == 0:
            self.stack.set_visible_child_name("empty")
        else:
            self.stack.set_visible_child_name("grid" if self.grid_button.get_active() else "list")

    def _match(self, item: AppItem, *_user_data) -> bool:
        r = item.record
        if self.source_filter and r.source.value != self.source_filter:
            return False
        if self.only_updates and item.update is None:
            return False
        if self.only_manual and r.package_manager != PackageManager.NONE:
            return False
        return all(term in item.search_text for term in self.query_terms)

    def _compare(self, a: AppItem, b: AppItem, *_user_data) -> int:
        ra, rb = a.record, b.record
        if self.sort_mode == "date":
            ka, kb = -(ra.effective_date or 0), -(rb.effective_date or 0)
        elif self.sort_mode == "source":
            ka, kb = SOURCE_PRIORITY.index(ra.source), SOURCE_PRIORITY.index(rb.source)
        elif self.sort_mode == "size":
            ka, kb = -(ra.size or 0), -(rb.size or 0)
        else:
            ka = kb = 0
        if ka != kb:
            return -1 if ka < kb else 1
        na, nb = ra.name.casefold(), rb.name.casefold()
        return (na > nb) - (na < nb)

    # -- filters ------------------------------------------------------------------------
    def set_source_filter(self, source: str) -> None:
        values = [v for v, _ in SOURCE_FILTERS]
        self.source_dropdown.set_selected(values.index(source) if source in values else 0)

    def reset_filters(self) -> None:
        self.search.set_text("")
        self.set_source_filter("")
        self.updates_toggle.set_active(False)
        self.manual_toggle.set_active(False)

    def _on_search(self, entry: Gtk.SearchEntry) -> None:
        self.query_terms = entry.get_text().casefold().split()
        self.filter.changed(Gtk.FilterChange.DIFFERENT)

    def _on_source_changed(self, dropdown, _pspec) -> None:
        self.source_filter = SOURCE_FILTERS[dropdown.get_selected()][0]
        self.refresh()
        self.filter.changed(Gtk.FilterChange.DIFFERENT)

    def _on_toggle_filter(self, _button) -> None:
        self.only_updates = self.updates_toggle.get_active()
        self.only_manual = self.manual_toggle.get_active()
        self.filter.changed(Gtk.FilterChange.DIFFERENT)

    def _on_sort_changed(self, dropdown, _pspec) -> None:
        self.sort_mode = SORT_MODES[dropdown.get_selected()][0]
        self.sorter.changed(Gtk.SorterChange.DIFFERENT)

    def _on_view_toggled(self, _button) -> None:
        mode = "list" if self.list_button.get_active() else "grid"
        self.controller.settings.set("view_mode", mode)
        self._update_count()

    # -- selection ------------------------------------------------------------------------
    def _on_select_mode(self, button: Gtk.ToggleButton) -> None:
        self.selection_mode = button.get_active()
        if not self.selection_mode:
            self.selected_keys.clear()
        self._rebind_visible()
        self._update_selection_ui()

    def set_selected(self, key: str, selected: bool) -> None:
        if selected:
            self.selected_keys.add(key)
        else:
            self.selected_keys.discard(key)
        self._update_selection_ui()

    def select_all(self) -> None:
        for i in range(self.sort_model.get_n_items()):
            self.selected_keys.add(self.sort_model.get_item(i).record.key)
        self._rebind_visible()
        self._update_selection_ui()

    def clear_selection(self) -> None:
        self.selected_keys.clear()
        self._rebind_visible()
        self._update_selection_ui()

    def _rebind_visible(self) -> None:
        # Force rows to rebind so checkboxes reflect the current state.
        self.sorter.changed(Gtk.SorterChange.DIFFERENT)
        self.filter.changed(Gtk.FilterChange.DIFFERENT)

    def _selected_records(self) -> list[AppRecord]:
        inv = self.controller.inventory
        return [inv.records[k] for k in self.selected_keys if k in inv.records]

    def _update_selection_ui(self) -> None:
        n = len(self.selected_keys)
        self.action_bar.set_revealed(self.selection_mode)
        self.selected_label.set_label(f"{n} selected")
        self.uninstall_selected.set_sensitive(n > 0)
        updates = self.controller.inventory.updates_for_records()
        self.update_selected.set_sensitive(any(k in updates for k in self.selected_keys))

    def _on_activate(self, _view, position: int) -> None:
        item = self.sort_model.get_item(position)
        if item is None:
            return
        if self.selection_mode:
            self.set_selected(item.record.key, item.record.key not in self.selected_keys)
            self._rebind_visible()
        else:
            self.emit("show-app", item.record.key)
