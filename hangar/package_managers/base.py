"""Common provider interface and shared scan context."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..core.application_model import AppRecord, UpdateInfo

if TYPE_CHECKING:
    from ..core.desktop_entry import DesktopIndex
    from .apt import DpkgDatabase


def current_desktops() -> list[str]:
    return [d for d in os.environ.get("XDG_CURRENT_DESKTOP", "").split(":") if d]


def shown_in_current_desktop(only_show_in: list[str], not_show_in: list[str]) -> bool:
    desktops = current_desktops()
    if only_show_in and desktops and not any(d in only_show_in for d in desktops):
        return False
    if not_show_in and any(d in not_show_in for d in desktops):
        return False
    return True


@dataclass
class ScanContext:
    """Data shared between providers during a single inventory scan."""

    desktop_index: "DesktopIndex"
    dpkg: "DpkgDatabase | None" = None
    home: str = field(default_factory=lambda: os.path.expanduser("~"))
    appimage_dirs: list[str] = field(default_factory=list)
    manual_dirs: list[str] = field(default_factory=list)
    # Paths (desktop files, executables, directories) already attributed to an
    # installation by an earlier provider; later providers skip them.
    claimed: set[str] = field(default_factory=set)
    # Desktop ids provided by package managers, used to recognise user
    # overrides of packaged launchers.
    managed_desktop_ids: set[str] = field(default_factory=set)
    # Records produced so far (earlier providers), for attaching related files.
    records: list[AppRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def claim(self, *paths: str) -> None:
        for p in paths:
            if p:
                self.claimed.add(p)
                real = os.path.realpath(p)
                if real != p:
                    self.claimed.add(real)

    def is_claimed(self, path: str) -> bool:
        return path in self.claimed or os.path.realpath(path) in self.claimed


class Provider:
    """A source of installed applications."""

    id = "base"
    label = "Base"
    icon_name = "package-x-generic-symbolic"
    description = ""

    def available(self) -> bool:
        raise NotImplementedError

    def tool_version(self) -> str:
        return ""

    def scan(self, ctx: ScanContext) -> list[AppRecord]:
        raise NotImplementedError

    def check_updates(self, records: list[AppRecord]) -> list[UpdateInfo]:
        return []

    def supports_updates(self) -> bool:
        return False
