"""Data model shared by discovery, inventory, services and UI."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Source(str, Enum):
    APT = "apt"
    SNAP = "snap"
    FLATPAK = "flatpak"
    APPIMAGE = "appimage"
    MANUAL = "manual"
    VENDOR = "vendor"
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        return SOURCE_LABELS[self]


SOURCE_LABELS = {
    Source.APT: "APT",
    Source.SNAP: "Snap",
    Source.FLATPAK: "Flatpak",
    Source.APPIMAGE: "AppImage",
    Source.MANUAL: "Manual",
    Source.VENDOR: "Vendor",
    Source.UNKNOWN: "Unknown",
}

# Preferred installation when the same application is installed several times.
SOURCE_PRIORITY = [
    Source.APT,
    Source.VENDOR,
    Source.FLATPAK,
    Source.SNAP,
    Source.APPIMAGE,
    Source.MANUAL,
    Source.UNKNOWN,
]


class Confidence(str, Enum):
    MANAGED = "managed"      # a package manager knows the application
    DETECTED = "detected"    # found by filesystem scanning with good evidence
    UNKNOWN = "unknown"      # application-like files without enough information

    @property
    def label(self) -> str:
        return {"managed": "Managed", "detected": "Detected", "unknown": "Unknown"}[self.value]


class Classification(str, Enum):
    """Manual review decision persisted by the user."""

    APPLICATION = "application"
    COMPONENT = "component"
    IGNORE = "ignore"


class PackageManager(str, Enum):
    APT = "apt"
    SNAP = "snap"
    FLATPAK = "flatpak"
    NONE = "none"

    @property
    def label(self) -> str:
        return {"apt": "APT / dpkg", "snap": "Snap", "flatpak": "Flatpak", "none": "None"}[self.value]


# Freedesktop main categories mapped to human friendly names.
CATEGORY_NAMES = {
    "AudioVideo": "Multimedia",
    "Audio": "Multimedia",
    "Video": "Multimedia",
    "Development": "Development",
    "Education": "Education",
    "Game": "Games",
    "Graphics": "Graphics",
    "Network": "Internet",
    "Office": "Office",
    "Science": "Science",
    "Settings": "Settings",
    "System": "System",
    "Utility": "Utilities",
}


def primary_category(categories: list[str]) -> str:
    for cat in categories:
        if cat in CATEGORY_NAMES:
            return CATEGORY_NAMES[cat]
    return "Other"


@dataclass
class UpdateInfo:
    """An available update reported by a package manager."""

    source: str                 # PackageManager value: apt / snap / flatpak
    package_name: str           # apt package, snap name, or flatpak ref
    name: str
    current_version: str
    new_version: str
    download_size: int | None = None
    installation: str = ""      # flatpak: system/user
    is_application: bool = False
    origin: str = ""
    phased: bool = False        # Ubuntu phased update (may be held back by apt)
    app_keys: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)   # e.g. GitHub release URL and asset

    @property
    def key(self) -> str:
        if self.installation:
            return f"{self.source}:{self.installation}:{self.package_name}"
        return f"{self.source}:{self.package_name}"

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UpdateInfo":
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})


@dataclass
class AppRecord:
    """One installation of one application."""

    key: str
    name: str
    source: Source
    package_manager: PackageManager
    confidence: Confidence
    summary: str = ""
    description: str = ""
    icon: str = ""
    package_name: str = ""
    version: str = ""
    install_date: float | None = None
    install_path: str = ""
    exec_path: str = ""
    exec_command: str = ""
    desktop_file: str = ""
    desktop_id: str = ""
    categories: list[str] = field(default_factory=list)
    size: int | None = None
    origin: str = ""
    homepage: str = ""
    is_component: bool = False
    hidden: bool = False
    has_launcher: bool = True
    related_paths: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    # Filled in by the inventory after discovery:
    classification: Classification | None = None
    group_id: str = ""
    first_seen: float | None = None

    @property
    def category(self) -> str:
        if not self.has_launcher and not self.categories:
            return "Command Line"
        return primary_category(self.categories)

    @property
    def is_manual(self) -> bool:
        return self.package_manager == PackageManager.NONE

    @property
    def source_label(self) -> str:
        return self.source.label

    @property
    def effective_date(self) -> float | None:
        return self.install_date or self.first_seen

    def to_dict(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        data["source"] = self.source.value
        data["package_manager"] = self.package_manager.value
        data["confidence"] = self.confidence.value
        data["classification"] = self.classification.value if self.classification else None
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppRecord":
        names = {f.name for f in dataclasses.fields(cls)}
        values = {k: v for k, v in data.items() if k in names}
        values["source"] = Source(values["source"])
        values["package_manager"] = PackageManager(values["package_manager"])
        values["confidence"] = Confidence(values["confidence"])
        cls_value = values.get("classification")
        values["classification"] = Classification(cls_value) if cls_value else None
        return cls(**values)

    def info_text(self) -> str:
        """Plain-text summary used by 'Copy package information'."""
        lines = [
            f"Name: {self.name}",
            f"Source: {self.source.label}",
            f"Package manager: {self.package_manager.label}",
        ]
        if self.package_name:
            lines.append(f"Package: {self.package_name}")
        if self.version:
            lines.append(f"Version: {self.version}")
        if self.origin:
            lines.append(f"Origin: {self.origin}")
        if self.install_path:
            lines.append(f"Location: {self.install_path}")
        if self.exec_path:
            lines.append(f"Executable: {self.exec_path}")
        if self.desktop_file:
            lines.append(f"Desktop entry: {self.desktop_file}")
        if self.homepage:
            lines.append(f"Homepage: {self.homepage}")
        return "\n".join(lines)


def format_size(size: int | None) -> str:
    if size is None or size < 0:
        return ""
    value = float(size)
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if value < 1000 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1000
    return ""


def parse_size(text: str) -> int | None:
    """Parse human readable sizes such as '596.5 MB' or '789.5 kB'."""
    text = (text or "").strip().replace(" ", " ")
    if not text:
        return None
    units = {"b": 1, "bytes": 1, "kb": 1000, "mb": 1000**2, "gb": 1000**3, "tb": 1000**4,
             "kib": 1024, "mib": 1024**2, "gib": 1024**3, "tib": 1024**4}
    parts = text.split()
    try:
        if len(parts) == 1:
            return int(float(parts[0]))
        return int(float(parts[0]) * units[parts[1].lower()])
    except (ValueError, KeyError):
        return None
