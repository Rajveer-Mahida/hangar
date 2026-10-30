"""Work that used to block the UI thread: page rebuilds and database writes."""

import threading

from gi.repository import GLib

from uam.core.application_model import AppRecord, Confidence, PackageManager, Source
from uam.core.database import Database
from uam.core.inventory import Inventory
from uam.core.scanner import ScanResult
from uam.core.settings import Settings


def rec(key):
    return AppRecord(key=key, name=key, source=Source.APT, package_manager=PackageManager.APT,
                     confidence=Confidence.MANAGED, package_name=key)


def test_scan_is_persisted_in_a_worker_and_applied_on_main(tmp_path):
    db = Database(str(tmp_path / "inv.db"))
    inventory = Inventory(db, Settings.load(db))
    result = ScanResult(records=[rec(f"apt:p{i}") for i in range(300)])
    out = {}
    worker = threading.Thread(target=lambda: out.setdefault("p", inventory.persist_scan(result)))
    worker.start()
    worker.join(30)
    assert inventory.records == {}                      # worker did not touch in-memory state
    diff = inventory.apply_scan(result, out["p"])
    assert len(inventory.records) == 300 and diff.first_scan
    assert len(db.load_inventory()) == 300              # main-thread connection sees worker's writes


def test_main_thread_can_read_while_a_worker_writes(tmp_path):
    db = Database(str(tmp_path / "inv.db"))
    db.add_history("scan", "before", "info")
    started, release = threading.Event(), threading.Event()

    def writer():
        with db.conn:  # hold an open write transaction on the worker's own connection
            db.conn.execute("INSERT INTO history (ts, action, target, status) VALUES (0, 'x', 'y', 'info')")
            started.set()
            release.wait(10)

    t = threading.Thread(target=writer)
    t.start()
    assert started.wait(10)
    entries = db.history()                              # must not wait for the writer (WAL)
    release.set()
    t.join(10)
    assert [e["target"] for e in entries] == ["before"]


class FakePage:
    """Minimal stand-in for a Gtk widget using the LazyRefresh mixin."""

    def __init__(self):
        from uam.ui.widgets import LazyRefresh

        self.__class__ = type("Page", (FakePage, LazyRefresh), {})
        self.mapped = False
        self.refreshes = 0
        self.map_cb = None
        self.init_lazy_refresh()

    def connect(self, signal, cb):
        assert signal == "map"
        self.map_cb = cb

    def get_mapped(self):
        return self.mapped

    def refresh(self):
        self.refreshes += 1


def drain():
    ctx = GLib.MainContext.default()
    while ctx.pending():
        ctx.iteration(False)


def test_hidden_pages_do_not_rebuild_until_shown():
    import uam.ui  # noqa: F401  (GI versions)

    page = FakePage()
    for _ in range(5):
        page.queue_refresh()
    drain()
    assert page.refreshes == 0                          # hidden: only marked stale
    page.mapped = True
    page.map_cb()
    drain()
    assert page.refreshes == 1                          # rebuilt once when shown


def test_bursts_of_changes_rebuild_visible_page_once():
    import uam.ui  # noqa: F401

    page = FakePage()
    page.mapped = True
    for _ in range(4):
        page.queue_refresh()
    drain()
    assert page.refreshes == 1
