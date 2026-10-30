"""Bottom bar showing the running package operation and its live output."""

from __future__ import annotations

from gi.repository import GLib, Gtk

from ..services.operation_service import Operation, OperationManager


class OperationBar(Gtk.Revealer):
    __gtype_name__ = "UamOperationBar"

    def __init__(self, manager: OperationManager):
        super().__init__(transition_type=Gtk.RevealerTransitionType.SLIDE_UP, reveal_child=False)
        self.manager = manager
        self.op: Operation | None = None
        self._op_handler = 0
        self._pulse_id = 0
        self._hide_id = 0

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        outer.add_css_class("operation-bar")
        row = Gtk.Box(spacing=12)
        self.icon = Gtk.Image(icon_name="emblem-synchronizing-symbolic")
        row.append(self.icon)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        self.title = Gtk.Label(xalign=0, ellipsize=3)
        self.title.add_css_class("heading")
        texts.append(self.title)
        self.message = Gtk.Label(xalign=0, ellipsize=3)
        self.message.add_css_class("dim-label")
        self.message.add_css_class("caption")
        texts.append(self.message)
        row.append(texts)
        self.queue_label = Gtk.Label()
        self.queue_label.add_css_class("dim-label")
        row.append(self.queue_label)
        self.details = Gtk.ToggleButton(icon_name="utilities-terminal-symbolic", tooltip_text="Show output")
        self.details.add_css_class("flat")
        self.details.connect("toggled", self._on_details)
        row.append(self.details)
        self.cancel = Gtk.Button(icon_name="process-stop-symbolic", tooltip_text="Cancel")
        self.cancel.add_css_class("flat")
        self.cancel.connect("clicked", self._on_cancel)
        row.append(self.cancel)
        self.close_button = Gtk.Button(icon_name="window-close-symbolic", tooltip_text="Dismiss", visible=False)
        self.close_button.add_css_class("flat")
        self.close_button.connect("clicked", lambda *_: self.set_reveal_child(False))
        row.append(self.close_button)
        outer.append(row)
        self.progress = Gtk.ProgressBar()
        outer.append(self.progress)

        self.log_revealer = Gtk.Revealer(reveal_child=False)
        self.log_view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                                     wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.log_view.add_css_class("operation-log")
        sw = Gtk.ScrolledWindow(min_content_height=160, max_content_height=220, propagate_natural_height=True)
        sw.set_child(self.log_view)
        self.log_scroll = sw
        self.log_revealer.set_child(sw)
        outer.append(self.log_revealer)
        self.set_child(outer)

        manager.connect("queue-changed", self._on_queue_changed)

    def _on_queue_changed(self, manager) -> None:
        current = manager.current
        if current is not None and current is not self.op:
            self._attach(current)
        waiting = len(manager.queue)
        self.queue_label.set_label(f"{waiting} more queued" if waiting else "")

    def _attach(self, op: Operation) -> None:
        if self.op is not None and self._op_handler:
            self.op.disconnect(self._op_handler)
        if self._hide_id:
            GLib.source_remove(self._hide_id)
            self._hide_id = 0
        self.op = op
        self._op_handler = op.connect("changed", self._on_changed)
        self.log_view.get_buffer().set_text("")
        self.close_button.set_visible(False)
        self.set_reveal_child(True)
        self._on_changed(op)

    def _on_changed(self, op: Operation) -> None:
        self.title.set_label(op.title)
        self.message.set_label(op.message or "")
        self.cancel.set_visible(not op.finished and not op.privileged)
        if op.finished:
            self._stop_pulse()
            self.progress.set_fraction(1.0 if op.status == "success" else 0.0)
            icon = {"success": "emblem-ok-symbolic", "failed": "dialog-error-symbolic",
                    "cancelled": "process-stop-symbolic"}[op.status]
            self.icon.set_from_icon_name(icon)
            self.close_button.set_visible(True)
            if op.status == "success" and not self.details.get_active():
                self._hide_id = GLib.timeout_add_seconds(4, self._auto_hide)
        else:
            self.icon.set_from_icon_name("emblem-synchronizing-symbolic")
            if op.fraction is None:
                self._start_pulse()
            else:
                self._stop_pulse()
                self.progress.set_fraction(op.fraction)
        if self.details.get_active():
            self._sync_log()

    def _sync_log(self) -> None:
        if self.op is None:
            return
        buf = self.log_view.get_buffer()
        buf.set_text("\n".join(self.op.log))
        end = buf.get_end_iter()
        mark = buf.create_mark(None, end, False)
        self.log_view.scroll_mark_onscreen(mark)
        buf.delete_mark(mark)

    def _on_details(self, button: Gtk.ToggleButton) -> None:
        self.log_revealer.set_reveal_child(button.get_active())
        if button.get_active():
            self._sync_log()

    def _start_pulse(self) -> None:
        if not self._pulse_id:
            self._pulse_id = GLib.timeout_add(120, self._pulse)

    def _stop_pulse(self) -> None:
        if self._pulse_id:
            GLib.source_remove(self._pulse_id)
            self._pulse_id = 0

    def _pulse(self) -> bool:
        self.progress.pulse()
        return True

    def _auto_hide(self) -> bool:
        self._hide_id = 0
        if self.manager.current is None and not self.details.get_active():
            self.set_reveal_child(False)
        return False

    def _on_cancel(self, _button) -> None:
        if self.op is not None:
            self.manager.cancel(self.op)
