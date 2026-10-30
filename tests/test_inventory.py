"""Inventory persistence, review classifications, history and settings."""

import sqlite3

from uam.core.application_model import (
    AppRecord,
    Classification,
    Confidence,
    PackageManager,
    Source,
    UpdateInfo,
    format_size,
    parse_size,
)
from uam.core.database import HISTORY_DETAIL_MAX, Database
from uam.core.inventory import Inventory
from uam.core.scanner import ScanResult
from uam.core.settings import Settings


def rec(key, name, source=Source.APT, pm=PackageManager.APT, confidence=Confidence.MANAGED, **kw):
    return AppRecord(key=key, name=name, source=source, package_manager=pm, confidence=confidence, **kw)


def make_inventory():
    db = Database(":memory:")
    return Inventory(db, Settings.load(db)), db


def test_record_roundtrip():
    r = rec("apt:x:x.desktop", "X", package_name="x", categories=["Utility"], extra={"a": 1})
    r.classification = Classification.COMPONENT
    assert AppRecord.from_dict(r.to_dict()) == r


def test_save_inventory_reports_new_and_removed():
    _, db = make_inventory()
    new, removed = db.save_inventory([rec("a", "A"), rec("b", "B")])
    assert new == {"a", "b"} and removed == set()
    new, removed = db.save_inventory([rec("b", "B"), rec("c", "C")])
    assert new == {"c"} and removed == {"a"}
    keys = {r.key for r in db.load_inventory()}
    assert keys == {"b", "c"}


def test_first_seen_is_preserved():
    _, db = make_inventory()
    db.save_inventory([rec("a", "A")])
    first = db.load_inventory()[0].first_seen
    db.save_inventory([rec("a", "A renamed")])
    loaded = db.load_inventory()[0]
    assert loaded.first_seen == first and loaded.name == "A renamed"


def test_classification_persists_and_controls_listing():
    inv, db = make_inventory()
    unknown = rec("manual:/usr/local/bin/tool", "tool", Source.MANUAL, PackageManager.NONE, Confidence.UNKNOWN)
    app = rec("apt:a:a.desktop", "A")
    inv.apply_scan(ScanResult(records=[unknown, app]))
    assert not inv.is_listed(unknown) and inv.needs_review(unknown)
    inv.classify(unknown, Classification.APPLICATION)
    assert inv.is_listed(unknown) and not inv.needs_review(unknown)
    # A new scan re-applies stored reviews.
    inv.apply_scan(ScanResult(records=[
        rec("manual:/usr/local/bin/tool", "tool", Source.MANUAL, PackageManager.NONE, Confidence.UNKNOWN), app]))
    assert inv.records["manual:/usr/local/bin/tool"].classification == Classification.APPLICATION
    inv.classify(inv.records["apt:a:a.desktop"], Classification.IGNORE)
    assert not inv.is_listed(inv.records["apt:a:a.desktop"])
    assert db.classifications()["apt:a:a.desktop"] == Classification.IGNORE


def test_hidden_and_component_visibility_follow_settings():
    inv, _ = make_inventory()
    hidden = rec("h", "Hidden", hidden=True)
    comp = rec("c", "Runtime", is_component=True)
    inv.apply_scan(ScanResult(records=[hidden, comp]))
    assert not inv.is_listed(hidden) and not inv.is_listed(comp)
    inv.settings.include_hidden = True
    inv.settings.show_components = True
    assert inv.is_listed(hidden) and inv.is_listed(comp)


def test_updates_are_matched_to_records():
    inv, _ = make_inventory()
    app = rec("apt:firefox:firefox.desktop", "Firefox", package_name="firefox")
    flat = rec("flatpak:system:org.x.Y/x86_64/stable", "Y", Source.FLATPAK, PackageManager.FLATPAK,
               package_name="org.x.Y", extra={"ref": "org.x.Y/x86_64/stable", "installation": "system"})
    inv.apply_scan(ScanResult(records=[app, flat]))
    inv.set_updates("apt", [UpdateInfo("apt", "firefox", "Firefox", "1", "2", is_application=True)])
    inv.set_updates("flatpak", [UpdateInfo("flatpak", "org.x.Y/x86_64/stable", "Y", "1", "2",
                                           installation="system", is_application=True)])
    assert inv.update_for(app).new_version == "2"
    assert inv.update_for(flat) is not None
    assert set(inv.updates_for_records()) == {app.key, flat.key}
    assert inv.counts()["updates"] == 2


def test_history_is_sanitized_and_bounded():
    _, db = make_inventory()
    db.add_history("remove", "Thing", "failed", "APT", "line one\nline two\t" + "x" * 1000)
    entry = db.history()[0]
    assert "\n" not in entry["detail"] and len(entry["detail"]) <= HISTORY_DETAIL_MAX
    db.clear_history()
    assert db.history() == []


def test_settings_roundtrip_and_validation():
    db = Database(":memory:")
    s = Settings.load(db)
    s.set("theme", "dark")
    s.set("scan_interval_minutes", 2)  # clamped on load
    s.set_provider("snap", False)
    db.set_setting("include_hidden", "yes")  # wrong type is ignored
    loaded = Settings.load(db)
    assert loaded.theme == "dark"
    assert loaded.scan_interval_minutes == 5
    assert loaded.provider_enabled("snap") is False and loaded.provider_enabled("apt") is True
    assert loaded.include_hidden is False


def test_backup_creates_valid_database(tmp_path):
    db = Database(str(tmp_path / "live.db"))
    db.add_history("scan", "x", "info")
    target = tmp_path / "backup.db"
    db.backup(str(target))
    conn = sqlite3.connect(str(target))
    assert conn.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 1
    conn.close()


def test_size_helpers():
    assert parse_size("596.5 MB") == 596_500_000
    assert parse_size("789.5 kB") == 789_500
    assert parse_size("") is None
    assert format_size(1_500_000) == "1.5 MB"


def test_data_folder_moves_to_new_name(tmp_path, monkeypatch):
    from uam.core.database import default_db_path

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    legacy = tmp_path / "unified-app-manager"
    legacy.mkdir()
    Database(str(legacy / "inventory.db")).add_history("scan", "kept", "info")
    path = default_db_path()
    assert path == str(tmp_path / "hangar" / "inventory.db") and not legacy.exists()
    assert Database(path).history()[0]["target"] == "kept"   # history survives the rename
