"""Application controller: scanning, update checks and change monitoring.

Long-running work happens in worker threads; results are applied on the
GLib main loop and announced through GObject signals.
"""

from __future__ import annotations

import logging
import os
import threading
import time

from gi.repository import Gio, GLib, GObject

from ..core.application_model import AppRecord, Classification
from ..core.database import Database
from ..core.desktop_entry import application_dirs
from ..core.inventory import Inventory
from ..core.scanner import Scanner
from ..core.settings import Settings
from .operation_service import Operation, OperationManager
from .update_service import SOURCE_LABELS, UPDATE_SOURCES, check_updates

log = logging.getLogger(__name__)

# Operation.source holds the display label ("Snap"); update checks take the key ("snap").
_SOURCE_KEYS = {label: key for key, label in SOURCE_LABELS.items()}


class AppController(GObject.Object):
    __gtype_name__ = "UamAppController"
    __gsignals__ = {
        "inventory-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "updates-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "scan-state": (GObject.SignalFlags.RUN_FIRST, None, (bool, str, float)),
        "update-check-state": (GObject.SignalFlags.RUN_FIRST, None, (bool,)),
        "history-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "notify-user": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, db: Database | None = None):
        super().__init__()
        self.db = db or Database()
        self.settings = Settings.load(self.db)
        self.inventory = Inventory(self.db, self.settings)
        self.operations = OperationManager(self.db)
        self.operations.connect("operation-finished", self._on_operation_finished)
        self.scanning = False
        self.checking_updates = False
        self.update_errors: dict[str, str] = {}
        self._rescan_requested = False
        self._timer_id = 0
        self._monitors: list[Gio.FileMonitor] = []
        self._debounce_id = 0

    # -- lifecycle -----------------------------------------------------------------
    def start(self) -> None:
        self.inventory.load_cached()
        self.emit("inventory-changed")
        self.emit("updates-changed")
        self.rescan(reason="startup")
        self.reschedule()
        self.setup_monitors()

    def shutdown(self) -> None:
        if self._timer_id:
            GLib.source_remove(self._timer_id)
            self._timer_id = 0
        for m in self._monitors:
            m.cancel()
        self._monitors.clear()

    def reschedule(self) -> None:
        if self._timer_id:
            GLib.source_remove(self._timer_id)
            self._timer_id = 0
        if self.settings.auto_refresh:
            seconds = max(5, self.settings.scan_interval_minutes) * 60
            self._timer_id = GLib.timeout_add_seconds(seconds, self._on_timer)

    def _on_timer(self) -> bool:
        if not self.operations.busy:
            self.rescan(reason="scheduled")
        return True

    def setup_monitors(self) -> None:
        for m in self._monitors:
            m.cancel()
        self._monitors.clear()
        if not self.settings.watch_launchers:
            return
        dirs = application_dirs() + [os.path.expanduser(d) for d in self.settings.application_dirs]
        for directory in dict.fromkeys(dirs):
            if not os.path.isdir(directory):
                continue
            try:
                monitor = Gio.File.new_for_path(directory).monitor_directory(Gio.FileMonitorFlags.NONE, None)
            except GLib.Error:
                continue
            monitor.connect("changed", self._on_fs_changed)
            self._monitors.append(monitor)

    def _on_fs_changed(self, _monitor, file, _other, event) -> None:
        if event not in (Gio.FileMonitorEvent.CREATED, Gio.FileMonitorEvent.DELETED,
                         Gio.FileMonitorEvent.CHANGES_DONE_HINT, Gio.FileMonitorEvent.MOVED_IN,
                         Gio.FileMonitorEvent.MOVED_OUT, Gio.FileMonitorEvent.RENAMED):
            return
        name = file.get_basename() if file else ""
        if name and (name.endswith((".desktop", ".AppImage", ".appimage"))):
            if self._debounce_id:
                GLib.source_remove(self._debounce_id)
            self._debounce_id = GLib.timeout_add_seconds(4, self._debounced_rescan)

    def _debounced_rescan(self) -> bool:
        self._debounce_id = 0
        if not self.operations.busy:
            self.rescan(reason="change")
        return False

    # -- scanning -------------------------------------------------------------------
    def rescan(self, reason: str = "user") -> None:
        if self.scanning:
            self._rescan_requested = True
            return
        self.scanning = True
        self.emit("scan-state", True, "Starting scan", 0.0)
        scanner = Scanner(self.settings)

        def progress(message: str, fraction: float) -> None:
            GLib.idle_add(lambda: self.emit("scan-state", True, message, fraction) or False)

        def worker():
            try:
                result = scanner.scan(progress)
                # Database writes happen here, not on the main thread (they took
                # >100 ms and made scrolling stutter whenever a scan finished).
                persisted = self.inventory.persist_scan(result)
                error = None
            except Exception as exc:  # pragma: no cover - surfaced to the user
                log.exception("scan failed")
                result, persisted, error = None, None, str(exc)
            GLib.idle_add(self._scan_done, result, persisted, error, reason)

        threading.Thread(target=worker, daemon=True, name="inventory-scan").start()

    def _scan_done(self, result, persisted, error, reason) -> bool:
        self.scanning = False
        if error or result is None:
            self.emit("scan-state", False, f"Scan failed: {error}", 1.0)
            self.db.add_history("scan", "Inventory scan", "failed", detail=error or "")
            self.emit("history-changed")
        else:
            diff = self.inventory.apply_scan(result, persisted)
            if diff.first_scan:
                self.db.add_history("scan", "your system for the first time", "info",
                                    detail=f"{len(result.records)} items found")
            else:
                for record in diff.added:
                    if record.package_manager.value == "none" and not record.hidden:
                        self.db.add_history("discover", record.name, "info", record.source.label,
                                            f"Found {record.install_path}")
            if diff.added or diff.removed or diff.first_scan:
                self.emit("history-changed")
            self.emit("inventory-changed")
            self.emit("updates-changed")  # stale updates of removed software were dropped
            self.emit("scan-state", False, "Scan complete", 1.0)
            self._maybe_check_updates(reason)
        if self._rescan_requested:
            self._rescan_requested = False
            GLib.timeout_add_seconds(1, lambda: self.rescan("queued") or False)
        return False

    # -- updates ----------------------------------------------------------------------
    def _maybe_check_updates(self, reason: str) -> None:
        if not self.settings.auto_check_updates or self.checking_updates:
            return
        interval = self.settings.update_check_interval_hours * 3600
        due = []
        for source in UPDATE_SOURCES:
            if not self.settings.provider_enabled(source):
                continue
            last = self.db.updates_checked_at(source)
            # APT data is local (cheap); network sources follow the interval.
            if source == "apt" or last is None or time.time() - last > interval:
                due.append(source)
        if due:
            self.check_updates(due, quiet=True)

    def check_updates(self, sources: list[str] | None = None, quiet: bool = False) -> None:
        if self.checking_updates:
            return
        sources = [s for s in (sources or list(UPDATE_SOURCES)) if self.settings.provider_enabled(s)]
        if not sources:
            return
        self.checking_updates = True
        self.emit("update-check-state", True)
        records = list(self.inventory.records.values())

        def worker():
            result = check_updates(records, sources, db=self.db)
            for source, updates in result.updates.items():
                self.db.save_updates(source, updates)  # off the main thread
            GLib.idle_add(self._updates_done, result, quiet)

        threading.Thread(target=worker, daemon=True, name="update-check").start()

    def _updates_done(self, result, quiet: bool) -> bool:
        self.checking_updates = False
        for source, updates in result.updates.items():
            self.inventory.set_updates(source, updates, persist=False)
            self.update_errors.pop(source, None)
        self.update_errors.update(result.errors)
        if not quiet:
            n = len(self.inventory.updates)
            self.emit("notify-user", f"{n} update{'s' if n != 1 else ''} available" if n else
                      "Everything is up to date")
            for source, error in result.errors.items():
                self.db.add_history("update-check", source, "failed", source, error)
            self.emit("history-changed")
        self.emit("update-check-state", False)
        self.emit("updates-changed")
        return False

    # -- operations -----------------------------------------------------------------
    def submit(self, op: Operation) -> Operation:
        return self.operations.submit(op)

    def _on_operation_finished(self, _manager, op: Operation) -> None:
        self.emit("history-changed")
        if op.status == "failed" and op.kind == "update" and op.source in _SOURCE_KEYS:
            # Part of a failed batch may have gone through (snapd refreshes each snap on its own,
            # dpkg sets up every package it can), so ask the source again instead of guessing.
            self.check_updates([_SOURCE_KEYS[op.source]], quiet=True)
        if op.status != "success":
            return
        if op.kind == "remove":
            for key in op.app_keys:
                self.inventory.remove_record(key)
            self.emit("inventory-changed")
        if op.kind == "update":
            for key in op.update_keys:
                update = self.inventory.updates.get(key)
                if update:
                    self.inventory.remove_update(update)
            self.emit("updates-changed")
        if op.kind == "refresh":
            self.check_updates(["apt"], quiet=False)
        if not self.operations.busy:
            # Pick up version changes and removed launchers.
            GLib.timeout_add_seconds(2, lambda: self.rescan("operation") or False)

    # -- user actions -----------------------------------------------------------------
    def classify(self, record: AppRecord, kind: Classification | None) -> None:
        self.inventory.classify(record, kind)
        label = kind.value if kind else "unreviewed"
        self.db.add_history("classify", record.name, "success", record.source.label, f"Marked as {label}")
        self.emit("inventory-changed")
        self.emit("history-changed")

    def log_action(self, action: str, target: str, status: str = "success", source: str = "",
                   detail: str = "") -> None:
        self.db.add_history(action, target, status, source, detail)
        self.emit("history-changed")
