"""Runs all providers and produces one inventory snapshot.

The scanner is synchronous and safe to run in a worker thread. It never
requires root privileges and never modifies the filesystem.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Callable

from ..package_managers.apt import AptProvider, DpkgDatabase
from ..package_managers.appimage import DEFAULT_DIRS as APPIMAGE_DEFAULT_DIRS
from ..package_managers.appimage import AppImageProvider
from ..package_managers.base import Provider, ScanContext
from ..package_managers.flatpak import FlatpakProvider
from ..package_managers.manual import ManualProvider
from ..package_managers.snap import SnapProvider
from .application_model import AppRecord
from .desktop_entry import DesktopIndex
from .settings import Settings

log = logging.getLogger(__name__)


@dataclass
class ScanResult:
    records: list[AppRecord]
    errors: list[str] = field(default_factory=list)
    durations: dict[str, float] = field(default_factory=dict)
    provider_status: dict[str, str] = field(default_factory=dict)
    finished_at: float = field(default_factory=time.time)


def all_providers() -> list[Provider]:
    # Order matters: package managers claim their files before the
    # filesystem scanners look for unclaimed software.
    return [AptProvider(), SnapProvider(), FlatpakProvider(), AppImageProvider(), ManualProvider()]


class Scanner:
    def __init__(self, settings: Settings, providers: list[Provider] | None = None):
        self.settings = settings
        self.providers = providers if providers is not None else all_providers()

    def _appimage_dirs(self) -> list[str]:
        dirs = [os.path.expanduser(d) for d in APPIMAGE_DEFAULT_DIRS]
        dirs += [os.path.expanduser(d) for d in self.settings.application_dirs]
        dirs += [os.path.expanduser(d) for d in self.settings.custom_dirs]
        seen, result = set(), []
        for d in dirs:
            d = os.path.normpath(d)
            if d not in seen:
                seen.add(d)
                result.append(d)
        return result

    def scan(self, progress: Callable[[str, float], None] | None = None) -> ScanResult:
        def report(message: str, fraction: float) -> None:
            if progress:
                progress(message, fraction)

        durations: dict[str, float] = {}
        status: dict[str, str] = {}
        t0 = time.monotonic()
        report("Reading desktop launchers", 0.02)
        index = DesktopIndex()
        durations["desktop"] = time.monotonic() - t0

        dpkg = None
        if os.path.exists("/var/lib/dpkg/status"):
            t = time.monotonic()
            report("Reading the dpkg database", 0.08)
            try:
                dpkg = DpkgDatabase().load()
            except OSError as exc:
                log.warning("dpkg database unreadable: %s", exc)
            durations["dpkg"] = time.monotonic() - t

        ctx = ScanContext(
            desktop_index=index,
            dpkg=dpkg,
            appimage_dirs=self._appimage_dirs(),
            manual_dirs=[os.path.normpath(os.path.expanduser(d)) for d in self.settings.custom_dirs],
        )
        records: list[AppRecord] = []
        n = len(self.providers)
        for i, provider in enumerate(self.providers):
            if not self.settings.provider_enabled(provider.id):
                status[provider.id] = "disabled"
                continue
            if not provider.available():
                status[provider.id] = "unavailable"
                continue
            report(f"Scanning {provider.label}", 0.15 + 0.8 * i / n)
            t = time.monotonic()
            try:
                found = provider.scan(ctx)
                records.extend(found)
                ctx.records = records
                status[provider.id] = "ok"
            except Exception as exc:  # one broken provider must not break the inventory
                log.exception("%s scan failed", provider.label)
                ctx.errors.append(f"{provider.label}: {exc}")
                status[provider.id] = "error"
            durations[provider.id] = time.monotonic() - t
        report("Finishing", 0.98)
        # Keys must be unique; keep the first occurrence.
        unique: dict[str, AppRecord] = {}
        for r in records:
            unique.setdefault(r.key, r)
        return ScanResult(records=list(unique.values()), errors=ctx.errors, durations=durations,
                          provider_status=status)
