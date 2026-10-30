"""Confirmation dialogs: uninstall preview (single and bulk) and updates."""

from __future__ import annotations

import threading
from typing import Callable

from gi.repository import Adw, GLib, Gtk

from ..core.application_model import AppRecord, UpdateInfo, format_size
from ..services.privilege_service import PrivilegeError
from ..services.uninstall_service import PlanItem, UninstallPlan, build_plan, operations_for
from .widgets import app_image, esc, pill, source_badge

KIND_ICONS = {
    "package": "package-x-generic-symbolic",
    "snap": "application-x-addon-symbolic",
    "flatpak": "system-software-install-symbolic",
    "file": "text-x-generic-symbolic",
    "directory": "folder-symbolic",
    "launcher": "application-x-executable-symbolic",
    "link": "emblem-symbolic-link",
    "icon": "image-x-generic-symbolic",
    "data": "folder-documents-symbolic",
}

METHOD_TEXT = {
    "apt": "Removed with APT. Requires administrator authentication.",
    "snap": "Removed with snapd. Requires administrator authentication.",
    "flatpak": "Removed with Flatpak.",
    "files": "Removed by this app after safety checks.",
}


def _message_box(text: str, css: str, icon: str) -> Gtk.Box:
    box = Gtk.Box(spacing=10)
    box.add_css_class(css)
    box.append(Gtk.Image(icon_name=icon, valign=Gtk.Align.START))
    box.append(Gtk.Label(label=text, xalign=0, wrap=True, hexpand=True, selectable=True))
    return box


def _plan_widget(plan: UninstallPlan, on_change: Callable[[], None], window) -> Gtk.Box:
    """Widget showing everything a plan will do, with per-item selection where allowed."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
    for text in plan.blockers:
        box.append(_message_box(text, "danger-box", "dialog-error-symbolic"))
    for text in plan.warnings:
        box.append(_message_box(text, "note-box", "dialog-warning-symbolic"))
    if plan.vendor_uninstaller:
        row = Gtk.Box(spacing=8)
        btn = Gtk.Button(label="Show Vendor Uninstaller")
        btn.connect("clicked", lambda *_: window.show_in_files(plan.vendor_uninstaller))
        row.append(btn)
        box.append(row)

    if plan.items:
        title = Gtk.Label(label="What will be removed", xalign=0)
        title.add_css_class("heading")
        box.append(title)
        listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        listbox.add_css_class("boxed-list")
        for item in plan.items:
            listbox.append(_item_row(plan, item, on_change))
        box.append(listbox)

    if plan.supports_purge and plan.supported:
        purge = Adw.SwitchRow(title=esc(plan.purge_label), active=plan.purge)

        def on_purge(row, _p):
            plan.purge = row.get_active()
            on_change()

        purge.connect("notify::active", on_purge)
        lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        lb.add_css_class("boxed-list")
        lb.append(purge)
        box.append(lb)

    if plan.unused_after:
        expander = Adw.ExpanderRow(title="Dependencies left installed",
                                   subtitle=esc(f"{len(plan.unused_after)} package(s) become unused; "
                                                "they are not removed"))
        for name in plan.unused_after[:200]:
            expander.add_row(Adw.ActionRow(title=esc(name)))
        lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        lb.add_css_class("boxed-list")
        lb.append(expander)
        box.append(lb)

    method = METHOD_TEXT.get(plan.method, "")
    if plan.method == "files" and plan.requires_root:
        method += " System files require administrator authentication."
    infos = ([method] if method else []) + plan.info
    for text in infos:
        label = Gtk.Label(label=text, xalign=0, wrap=True)
        label.add_css_class("dim-label")
        label.add_css_class("caption")
        box.append(label)
    return box


def _item_row(plan: UninstallPlan, item: PlanItem, on_change) -> Adw.ActionRow:
    subtitle = item.description
    if item.size:
        subtitle += f" · {format_size(item.size)}"
    if item.blocked:
        subtitle = f"Will not be removed: {item.blocked}"
    elif item.requires_root and plan.method == "files":
        subtitle += " · system file"
    row = Adw.ActionRow(title=esc(item.target), subtitle=esc(subtitle), subtitle_lines=3)
    row.add_prefix(Gtk.Image.new_from_icon_name(KIND_ICONS.get(item.kind, "text-x-generic-symbolic")))
    toggleable = plan.method == "files" or item.optional
    check = Gtk.CheckButton(valign=Gtk.Align.CENTER, active=item.selected and item.removable)
    check.set_sensitive(toggleable and item.removable)
    if not toggleable:
        check.set_tooltip_text("Removed together with the package")

    def on_toggle(c):
        item.selected = c.get_active()
        on_change()

    check.connect("toggled", on_toggle)
    row.add_suffix(check)
    if item.blocked:
        row.add_suffix(pill("Kept", "warning"))
    return row


class UninstallDialog(Adw.Dialog):
    """Preview and confirm the removal of one installation."""

    def __init__(self, window, record: AppRecord, plan: UninstallPlan | None = None):
        super().__init__(title=f"Uninstall {record.name}", content_width=620, content_height=640)
        self.window = window
        self.controller = window.controller
        self.record = record
        self.plan: UninstallPlan | None = None

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16, margin_start=24, margin_end=24,
                            margin_top=6, margin_bottom=18)
        hero = Gtk.Box(spacing=14)
        hero.append(app_image(record, 56))
        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER)
        title = Gtk.Label(label=record.name, xalign=0, wrap=True)
        title.add_css_class("title-3")
        names.append(title)
        meta = Gtk.Box(spacing=6)
        meta.append(source_badge(record.source))
        if record.version:
            v = Gtk.Label(label=record.version)
            v.add_css_class("dim-label")
            meta.append(v)
        names.append(meta)
        hero.append(names)
        self.body.append(hero)
        self.plan_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.body.append(self.plan_area)
        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True, propagate_natural_height=True)
        sw.set_child(self.body)
        toolbar.set_content(sw)

        bottom = Gtk.Box(spacing=12, margin_start=24, margin_end=24, margin_top=12, margin_bottom=18)
        self.summary = Gtk.Label(xalign=0, hexpand=True, wrap=True)
        self.summary.add_css_class("dim-label")
        bottom.append(self.summary)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        bottom.append(cancel)
        self.confirm = Gtk.Button(label="Uninstall", sensitive=False)
        self.confirm.add_css_class("destructive-action")
        self.confirm.connect("clicked", self._on_confirm)
        bottom.append(self.confirm)
        toolbar.add_bottom_bar(bottom)
        self.set_child(toolbar)

        if plan is not None:
            self._show_plan(plan)
        else:
            self._show_loading()
            records = list(self.controller.inventory.records.values())

            def worker():
                try:
                    built = build_plan(record, records)
                    GLib.idle_add(self._show_plan, built)
                except Exception as exc:  # shown in the dialog
                    GLib.idle_add(self._show_error, str(exc))

            threading.Thread(target=worker, daemon=True).start()

    def _show_loading(self) -> None:
        box = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER, margin_top=40, margin_bottom=40)
        spinner = Gtk.Spinner(spinning=True)
        box.append(spinner)
        box.append(Gtk.Label(label="Preparing a preview of the removal…"))
        self.plan_area.append(box)
        self.summary.set_label("Nothing has been changed yet")

    def _clear(self) -> None:
        child = self.plan_area.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.plan_area.remove(child)
            child = nxt

    def _show_error(self, message: str) -> bool:
        self._clear()
        self.plan_area.append(_message_box(message, "danger-box", "dialog-error-symbolic"))
        self.confirm.set_sensitive(False)
        return False

    def _show_plan(self, plan: UninstallPlan) -> bool:
        self.plan = plan
        self._clear()
        self.plan_area.append(_plan_widget(plan, self._update_summary, self.window))
        self._update_summary()
        return False

    def _update_summary(self) -> None:
        plan = self.plan
        if plan is None:
            return
        ok = plan.supported and bool(plan.selected_items())
        self.confirm.set_sensitive(ok)
        if not plan.supported:
            self.summary.set_label("This item cannot be removed automatically")
        elif not plan.selected_items():
            self.summary.set_label("Nothing selected")
        else:
            size = plan.total_size
            self.summary.set_label(f"Frees about {format_size(size)}" if size else
                                   f"{len(plan.selected_items())} item(s) selected")

    def _on_confirm(self, _button) -> None:
        try:
            ops = operations_for(self.plan)
        except (PrivilegeError, ValueError) as exc:
            self._show_error(str(exc))
            return
        for op in ops:
            self.controller.submit(op)
        self.close()


class BulkUninstallDialog(Adw.Dialog):
    """Preview several removals; each installation keeps its own plan and safety checks."""

    def __init__(self, window, records: list[AppRecord]):
        super().__init__(title=f"Uninstall {len(records)} Applications", content_width=680, content_height=680)
        self.window = window
        self.controller = window.controller
        self.records = records
        self.plans: list[UninstallPlan] = []

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_start=24, margin_end=24,
                            margin_top=6, margin_bottom=18)
        spinner_box = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER, margin_top=40)
        spinner_box.append(Gtk.Spinner(spinning=True))
        self.progress = Gtk.Label(label="Preparing previews…")
        spinner_box.append(self.progress)
        self.body.append(spinner_box)
        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        sw.set_child(self.body)
        toolbar.set_content(sw)
        bottom = Gtk.Box(spacing=12, margin_start=24, margin_end=24, margin_top=12, margin_bottom=18)
        self.summary = Gtk.Label(xalign=0, hexpand=True, wrap=True)
        self.summary.add_css_class("dim-label")
        bottom.append(self.summary)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        bottom.append(cancel)
        self.confirm = Gtk.Button(label="Uninstall", sensitive=False)
        self.confirm.add_css_class("destructive-action")
        self.confirm.connect("clicked", self._on_confirm)
        bottom.append(self.confirm)
        toolbar.add_bottom_bar(bottom)
        self.set_child(toolbar)

        all_records = list(self.controller.inventory.records.values())

        def worker():
            plans = []
            for i, rec in enumerate(records):
                GLib.idle_add(lambda i=i, n=rec.name: self.progress.set_label(
                    f"Preparing preview {i + 1} of {len(records)}: {n}") or False)
                try:
                    plans.append(build_plan(rec, all_records))
                except Exception as exc:
                    plan = UninstallPlan(record=rec, method="unsupported")
                    plan.blockers.append(str(exc))
                    plans.append(plan)
            GLib.idle_add(self._show_plans, plans)

        threading.Thread(target=worker, daemon=True).start()

    def _show_plans(self, plans: list[UninstallPlan]) -> bool:
        self.plans = plans
        child = self.body.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.body.remove(child)
            child = nxt
        note = Gtk.Label(label="Each application is removed separately with its own safety checks. You may be "
                               "asked to authenticate for system changes.", xalign=0, wrap=True)
        note.add_css_class("dim-label")
        self.body.append(note)
        for plan in plans:
            listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
            listbox.add_css_class("boxed-list")
            status = "Will be skipped" if not plan.supported else (
                f"{len(plan.selected_items())} item(s)" + (" · warnings" if plan.warnings else ""))
            # Plans that delete files are always shown expanded so every path is visible.
            expander = Adw.ExpanderRow(title=esc(plan.record.name), subtitle=esc(status),
                                       expanded=bool(plan.warnings or plan.blockers or plan.method == "files"))
            expander.add_prefix(app_image(plan.record, 32))
            expander.add_suffix(source_badge(plan.record.source))
            inner = Gtk.Box(margin_start=12, margin_end=12, margin_top=12, margin_bottom=12)
            inner.append(_plan_widget(plan, self._update_summary, self.window))
            row = Gtk.ListBoxRow(activatable=False)
            row.set_child(inner)
            expander.add_row(row)
            listbox.append(expander)
            self.body.append(listbox)
        self._update_summary()
        return False

    def _ready(self) -> list[UninstallPlan]:
        return [p for p in self.plans if p.supported and p.selected_items()]

    def _update_summary(self) -> None:
        ready = self._ready()
        skipped = len(self.plans) - len(ready)
        size = sum(p.total_size for p in ready)
        text = f"{len(ready)} application(s) will be removed"
        if size:
            text += f", freeing about {format_size(size)}"
        if skipped:
            text += f" · {skipped} skipped"
        self.summary.set_label(text)
        self.confirm.set_sensitive(bool(ready))

    def _on_confirm(self, _button) -> None:
        errors = []
        for plan in self._ready():
            try:
                for op in operations_for(plan):
                    self.controller.submit(op)
            except (PrivilegeError, ValueError) as exc:
                errors.append(f"{plan.record.name}: {exc}")
        if errors:
            self.window.show_error("Some removals could not be started", "\n".join(errors))
        self.close()


def confirm_updates(window, updates: list[UpdateInfo], everything_apt: bool, on_confirm: Callable[[], None]) -> None:
    names = sorted({u.name for u in updates}, key=str.casefold)
    shown = ", ".join(names[:8]) + (f" and {len(names) - 8} more" if len(names) > 8 else "")
    size = sum(u.download_size or 0 for u in updates)
    body = shown
    if size:
        body += f"\n\nDownload size: about {format_size(size)}."
    if everything_apt and any(u.source == "apt" for u in updates):
        body += "\n\nAll upgradable APT packages are updated with 'apt-get upgrade'. It never removes packages; " \
                "updates that need new or removed dependencies are held back."
    if any(u.source in ("apt", "snap") or u.installation == "system" for u in updates):
        body += "\n\nAdministrator authentication is required."
    dialog = Adw.AlertDialog(heading=f"Install {len(updates)} Update{'s' if len(updates) != 1 else ''}?", body=body)
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("update", "Update")
    dialog.set_response_appearance("update", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("update")
    dialog.set_close_response("cancel")
    dialog.connect("response", lambda _d, r: on_confirm() if r == "update" else None)
    dialog.present(window)


def confirm_action(window, heading: str, body: str, action_label: str, on_confirm: Callable[[], None],
                   destructive: bool = False) -> None:
    dialog = Adw.AlertDialog(heading=heading, body=body)
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("ok", action_label)
    dialog.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE if destructive
                                   else Adw.ResponseAppearance.SUGGESTED)
    dialog.set_close_response("cancel")
    dialog.connect("response", lambda _d, r: on_confirm() if r == "ok" else None)
    dialog.present(window)
