"""User settings persisted in the SQLite database."""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field
from typing import Any

from .database import Database

PROVIDER_IDS = ("apt", "snap", "flatpak", "appimage", "manual", "github")


def _default_dirs() -> list[str]:
    home = os.path.expanduser("~")
    return [os.path.join(home, d) for d in ("Applications", "AppImages", "Apps", "Downloads", "bin")]


@dataclass
class Settings:
    theme: str = "system"                       # system / light / dark
    auto_refresh: bool = True
    scan_interval_minutes: int = 30
    application_dirs: list[str] = field(default_factory=_default_dirs)   # AppImage folders
    custom_dirs: list[str] = field(default_factory=list)                 # extra manual install roots
    providers: dict[str, bool] = field(default_factory=lambda: {p: True for p in PROVIDER_IDS})
    include_hidden: bool = False
    show_components: bool = False
    confirm_uninstall: bool = True
    group_duplicates: bool = True
    view_mode: str = "list"
    auto_check_updates: bool = True
    update_check_interval_hours: int = 6
    watch_launchers: bool = True

    _db: Database | None = field(default=None, repr=False, compare=False)

    @classmethod
    def load(cls, db: Database) -> "Settings":
        settings = cls()
        for f in dataclasses.fields(cls):
            if f.name.startswith("_"):
                continue
            value = db.get_setting(f.name, None)
            if value is None:
                continue
            default = getattr(settings, f.name)
            if isinstance(default, bool) and not isinstance(value, bool):
                continue
            if isinstance(default, int) and not isinstance(default, bool) and not isinstance(value, int):
                continue
            if isinstance(default, dict) and isinstance(value, dict):
                merged = dict(default)
                merged.update({k: bool(v) for k, v in value.items() if k in default})
                value = merged
            elif isinstance(default, list) and not isinstance(value, list):
                continue
            setattr(settings, f.name, value)
        settings.scan_interval_minutes = max(5, min(24 * 60, settings.scan_interval_minutes))
        settings._db = db
        return settings

    def set(self, name: str, value: Any) -> None:
        if name.startswith("_") or not hasattr(self, name):
            raise AttributeError(name)
        setattr(self, name, value)
        if self._db is not None:
            self._db.set_setting(name, value)

    def provider_enabled(self, provider_id: str) -> bool:
        return self.providers.get(provider_id, True)

    def set_provider(self, provider_id: str, enabled: bool) -> None:
        providers = dict(self.providers)
        providers[provider_id] = enabled
        self.set("providers", providers)
