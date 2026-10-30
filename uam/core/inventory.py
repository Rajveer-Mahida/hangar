"""The unified application inventory: scan results + reviews + updates."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .application_model import (
    AppRecord,
    Classification,
    Confidence,
    PackageManager,
    Source,
    UpdateInfo,
)
from .database import Database
from .duplicate_detector import assign_groups, pick_primary
from .scanner import ScanResult
from .settings import Settings


@dataclass
class ScanDiff:
    added: list[AppRecord] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    first_scan: bool = False


@dataclass
class PersistedScan:
    """A scan already written to the database (done in the scan worker thread)."""

    result: ScanResult
    first_scan: bool
    added_keys: set[str]
    removed_keys: set[str]
    reviews: dict[str, Classification]


def update_lookup_key(source: str, package_name: str, installation: str = "") -> str:
    return f"{source}:{installation}:{package_name}"


class Inventory:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.records: dict[str, AppRecord] = {}
        self.groups: dict[str, list[AppRecord]] = {}
        self.updates: dict[str, UpdateInfo] = {}
        self.last_scan: float | None = None
        self.scan_errors: list[str] = []
        self.provider_status: dict[str, str] = {}

    # -- loading --------------------------------------------------------------
    def load_cached(self) -> bool:
        records = self.db.load_inventory()
        self._set_records(records)
        for u in self.db.load_updates():
            self.updates[u.key] = u
        value = self.db.get_meta("last_scan")
        self.last_scan = float(value) if value else None
        return bool(records)

    def _set_records(self, records: list[AppRecord], reviews: dict[str, Classification] | None = None) -> None:
        if reviews is None:
            reviews = self.db.classifications()
        for r in records:
            r.classification = reviews.get(r.key)
        self.records = {r.key: r for r in records}
        self.groups = assign_groups(records)

    def persist_scan(self, result: ScanResult) -> PersistedScan:
        """Write a scan to the database. Safe to call from a worker thread; the
        in-memory inventory is not touched (see ``apply_scan``)."""
        first = self.db.inventory_is_empty() and self.db.get_meta("last_scan") is None
        added_keys, removed_keys = self.db.save_inventory(result.records)
        self.db.set_meta("last_scan", str(result.finished_at))
        return PersistedScan(result, first, added_keys, removed_keys, self.db.classifications())

    def apply_scan(self, result: ScanResult, persisted: PersistedScan | None = None) -> ScanDiff:
        """Make a scan the current inventory (main thread). Persists it first unless
        ``persisted`` comes from ``persist_scan`` in a worker thread."""
        if persisted is None:
            persisted = self.persist_scan(result)
        self._set_records(result.records, persisted.reviews)
        self.last_scan = result.finished_at
        self.scan_errors = list(result.errors)
        self.provider_status = dict(result.provider_status)
        diff = ScanDiff(first_scan=persisted.first_scan, removed=sorted(persisted.removed_keys))
        diff.added = [self.records[k] for k in sorted(persisted.added_keys) if k in self.records]
        # Drop update entries for packages that no longer exist.
        known = {self.update_key_for(r) for r in self.records.values()}
        for key, update in list(self.updates.items()):
            if update.source == "github":
                gone = not any(k in self.records for k in update.app_keys)
            else:
                gone = update.is_application and update_lookup_key(
                    update.source, update.package_name, update.installation) not in known
            if gone:
                self.updates.pop(key, None)
        return diff

    # -- updates ----------------------------------------------------------------
    @staticmethod
    def update_key_for(record: AppRecord) -> str:
        pm = record.package_manager
        if pm == PackageManager.FLATPAK:
            return update_lookup_key("flatpak", record.extra.get("ref", ""), record.extra.get("installation", ""))
        return update_lookup_key(pm.value, record.package_name)

    def set_updates(self, source: str, updates: list[UpdateInfo], persist: bool = True) -> None:
        """Replace the updates of one source. ``persist=False`` when a worker thread
        already called ``db.save_updates``."""
        for key in [k for k, u in self.updates.items() if u.source == source]:
            del self.updates[key]
        for u in updates:
            self.updates[u.key] = u
        if persist:
            self.db.save_updates(source, updates)

    def remove_update(self, update: UpdateInfo) -> None:
        self.updates.pop(update.key, None)
        self.db.remove_update(update.key)

    def _update_index(self) -> dict[str, UpdateInfo]:
        return {update_lookup_key(u.source, u.package_name, u.installation): u for u in self.updates.values()}

    def _github_index(self) -> dict[str, UpdateInfo]:
        return {key: u for u in self.updates.values() if u.source == "github" for key in u.app_keys}

    def update_for(self, record: AppRecord) -> UpdateInfo | None:
        github = self._github_index().get(record.key)
        if github is not None:
            return github
        if record.package_manager == PackageManager.NONE:
            return None
        return self._update_index().get(self.update_key_for(record))

    def updates_for_records(self) -> dict[str, UpdateInfo]:
        index = self._update_index()
        result = dict(self._github_index())
        for r in self.records.values():
            if r.package_manager != PackageManager.NONE:
                u = index.get(self.update_key_for(r))
                if u:
                    result[r.key] = u
        return {k: v for k, v in result.items() if k in self.records}

    # -- visibility rules -------------------------------------------------------
    def is_listed(self, record: AppRecord) -> bool:
        """Whether a record appears in 'All Applications'."""
        c = record.classification
        if c == Classification.IGNORE:
            return False
        if c == Classification.APPLICATION:
            return True
        if c == Classification.COMPONENT or record.is_component:
            return self.settings.show_components
        if record.confidence == Confidence.UNKNOWN:
            return False
        if record.hidden:
            return self.settings.include_hidden
        return True

    def needs_review(self, record: AppRecord) -> bool:
        return (record.package_manager == PackageManager.NONE and record.classification is None
                and record.confidence == Confidence.UNKNOWN)

    def manual_records(self) -> list[AppRecord]:
        return [r for r in self.records.values() if r.package_manager == PackageManager.NONE]

    def listed_records(self, group: bool | None = None) -> list[AppRecord]:
        group = self.settings.group_duplicates if group is None else group
        listed = [r for r in self.records.values() if self.is_listed(r)]
        if not group:
            return listed
        result, seen_groups = [], set()
        for r in listed:
            if r.group_id in self.groups:
                if r.group_id in seen_groups:
                    continue
                members = [m for m in self.groups[r.group_id] if self.is_listed(m)]
                seen_groups.add(r.group_id)
                result.append(pick_primary(members))
            else:
                result.append(r)
        return result

    def installations_of(self, record: AppRecord) -> list[AppRecord]:
        members = self.groups.get(record.group_id)
        return list(members) if members else [record]

    # -- statistics ---------------------------------------------------------------
    def counts(self) -> dict[str, int]:
        listed = [r for r in self.records.values() if self.is_listed(r)]
        counts = {s.value: 0 for s in Source}
        for r in listed:
            counts[r.source.value] += 1
        counts["total"] = len(listed)
        # Manually installed = no package manager, excluding AppImages (counted separately).
        counts["manual_all"] = sum(
            1 for r in listed if r.package_manager == PackageManager.NONE and r.source != Source.APPIMAGE)
        counts["updates"] = len(self.updates)
        counts["app_updates"] = sum(1 for u in self.updates.values() if u.is_application)
        counts["needs_review"] = sum(1 for r in self.records.values() if self.needs_review(r))
        counts["duplicates"] = len(self.groups)
        return counts

    def recent(self, limit: int = 8) -> list[AppRecord]:
        listed = [r for r in self.listed_records(group=False) if r.effective_date]
        listed.sort(key=lambda r: r.effective_date or 0, reverse=True)
        return listed[:limit]

    def classify(self, record: AppRecord, kind: Classification | None) -> None:
        record.classification = kind
        self.db.set_classification(record.key, kind, record.name)

    def remove_record(self, key: str) -> None:
        """Forget a record after a successful uninstall (until the next scan)."""
        record = self.records.pop(key, None)
        if record and record.group_id in self.groups:
            members = [m for m in self.groups[record.group_id] if m.key != key]
            if len(members) > 1:
                self.groups[record.group_id] = members
            else:
                self.groups.pop(record.group_id, None)
                for m in members:
                    m.group_id = m.key

    def age_of_scan(self) -> float | None:
        return time.time() - self.last_scan if self.last_scan else None
