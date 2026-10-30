"""Detect the same application installed through several sources.

Each installation produces identity tokens (normalised name, package/app-id
stem, executable name). Installations sharing a token are grouped with a
union-find structure. Generic tokens are ignored to avoid false merges.
"""

from __future__ import annotations

import os
import re

from .application_model import SOURCE_PRIORITY, AppRecord, PackageManager, Source

GENERIC_TOKENS = {
    "app", "apps", "application", "desktop", "client", "studio", "editor", "browser", "player",
    "manager", "terminal", "launcher", "gui", "main", "run", "start", "bin", "tool", "tools",
    "settings", "files", "viewer", "web", "electron", "java", "python", "python3", "wine", "env",
    "sh", "bash", "flatpak", "snap", "appimage", "default", "linux", "stable", "beta", "nightly",
    "chrome", "monitor", "update", "updater", "helper", "server", "service", "daemon",
}

# Records that describe launchers rather than installations.
_LAUNCHER_PREFIXES = ("webapp:", "launcher:", "link:")


def _norm(text: str) -> str:
    text = text.lower()
    text = re.sub(r"\((flatpak|snap|appimage|deb|beta|preview)\)", "", text)
    return re.sub(r"[^a-z0-9]", "", text)


def _stem(desktop_id: str) -> str:
    stem = desktop_id[:-8] if desktop_id.endswith(".desktop") else desktop_id
    if "_" in stem:  # snap desktop ids: <snap>_<app>
        stem = stem.split("_", 1)[0]
    return stem


def identity_tokens(record: AppRecord) -> set[str]:
    tokens: set[str] = set()

    def add(prefix: str, value: str) -> None:
        value = _norm(value)
        if len(value) >= 3 and value not in GENERIC_TOKENS:
            tokens.add(f"{prefix}:{value}")

    add("n", record.name)
    if record.desktop_id:
        stem = _stem(record.desktop_id)
        add("i", stem)
        # Reverse-DNS ids (com.visualstudio.code) also match their short name (code).
        if stem.count(".") == 2:
            add("i", stem.rsplit(".", 1)[-1])
    if record.package_manager == PackageManager.FLATPAK:
        add("i", record.package_name)
        if record.package_name.count(".") == 2:
            add("i", record.package_name.rsplit(".", 1)[-1])
    elif record.package_manager in (PackageManager.SNAP, PackageManager.APT):
        add("i", record.package_name.split(":", 1)[0])
    elif record.exec_path and record.source != Source.APPIMAGE:
        add("i", os.path.basename(record.exec_path))
    return tokens


class _UnionFind:
    def __init__(self):
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            if rb < ra:
                ra, rb = rb, ra
            self.parent[rb] = ra


def _installation_id(record: AppRecord) -> str:
    """Records with the same installation id are one installation."""
    if record.package_manager != PackageManager.NONE:
        return f"{record.package_manager.value}:{record.package_name}:{record.extra.get('installation', '')}"
    return record.key


def assign_groups(records: list[AppRecord]) -> dict[str, list[AppRecord]]:
    """Set ``group_id`` on every record and return groups with >1 installation."""
    uf = _UnionFind()
    owner_of_token: dict[str, str] = {}
    participants = [r for r in records
                    if not r.key.startswith(_LAUNCHER_PREFIXES) and not r.is_component and not r.hidden]
    for r in records:
        r.group_id = r.key
        uf.find(r.key)
    for r in participants:
        for token in identity_tokens(r):
            other = owner_of_token.get(token)
            if other is None:
                owner_of_token[token] = r.key
            else:
                uf.union(other, r.key)

    groups: dict[str, list[AppRecord]] = {}
    for r in records:
        root = uf.find(r.key)
        r.group_id = root
        groups.setdefault(root, []).append(r)

    multi: dict[str, list[AppRecord]] = {}
    for root, members in groups.items():
        installations = {_installation_id(m) for m in members}
        if len(installations) > 1:
            multi[root] = sorted(members, key=primary_sort_key)
        else:
            # Same installation with several launchers is not a duplicate.
            for m in members:
                m.group_id = m.key
    return multi


def primary_sort_key(record: AppRecord):
    try:
        rank = SOURCE_PRIORITY.index(record.source)
    except ValueError:
        rank = len(SOURCE_PRIORITY)
    return (record.hidden, not record.has_launcher, rank, record.name.lower(), record.key)


def pick_primary(members: list[AppRecord]) -> AppRecord:
    return sorted(members, key=primary_sort_key)[0]
