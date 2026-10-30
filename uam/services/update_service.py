"""Update checks (APT, Snap, Flatpak) and update operations."""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field

from ..core.application_model import AppRecord, UpdateInfo
from ..core.safety import validate_apt_package, validate_flatpak_ref, validate_snap_name
from ..package_managers.apt import AptProvider
from ..package_managers.base import Provider
from ..package_managers.flatpak import FlatpakProvider
from ..package_managers.snap import SnapProvider
from .github_service import GitHubReleaseChecker
from .operation_service import CommandStep, Operation
from .privilege_service import helper_argv

log = logging.getLogger(__name__)

UPDATE_SOURCES = ("apt", "snap", "flatpak", "github")
SOURCE_LABELS = {"apt": "APT", "snap": "Snap", "flatpak": "Flatpak", "github": "GitHub releases"}
# Sources installed by a package manager in one batch; GitHub releases are
# downloaded and installed one at a time through their own confirmation flow.
BATCH_SOURCES = ("apt", "snap", "flatpak")


@dataclass
class UpdateCheckResult:
    updates: dict[str, list[UpdateInfo]] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


def update_providers(db=None) -> dict[str, Provider]:
    providers = {"apt": AptProvider(), "snap": SnapProvider(), "flatpak": FlatpakProvider()}
    if db is not None:
        providers["github"] = GitHubReleaseChecker(db)
    return providers


def check_updates(records: list[AppRecord], sources: list[str],
                  providers: dict[str, Provider] | None = None, db=None) -> UpdateCheckResult:
    """Blocking; run in a worker thread. Does not require privileges.

    APT reads the local package lists (refresh them with `apt update` for
    fresh data); Snap and Flatpak ask their remote stores.
    """
    providers = providers or update_providers(db)
    result = UpdateCheckResult()
    for source in sources:
        provider = providers.get(source)
        if provider is None or not provider.available():
            continue
        try:
            result.updates[source] = provider.check_updates(records)
        except Exception as exc:
            log.warning("update check for %s failed: %s", source, exc)
            result.errors[source] = str(exc) or exc.__class__.__name__
    return result


def operations_for_updates(updates: list[UpdateInfo], everything_apt: bool = False) -> list[Operation]:
    """Batch updates per package manager (one authentication per batch).

    GitHub release updates are not included; they need a download first.
    """
    updates = [u for u in updates if u.source in BATCH_SOURCES]
    ops: list[Operation] = []
    apt = [u for u in updates if u.source == "apt"]
    snaps = [u for u in updates if u.source == "snap"]
    flatpaks = [u for u in updates if u.source == "flatpak"]

    if apt:
        if everything_apt:
            argv = helper_argv("apt-upgrade-all", [])
            title = "Updating all APT packages"
        else:
            names = [validate_apt_package(u.package_name) for u in apt]
            argv = helper_argv("apt-upgrade", names)
            title = f"Updating {_names(apt)}"
        ops.append(Operation(title, "update", [u.name for u in apt], [CommandStep(argv, title, privileged=True)],
                             source="APT", app_keys=[k for u in apt for k in u.app_keys],
                             update_keys=[u.key for u in apt]))
    if snaps:
        names = [validate_snap_name(u.package_name) for u in snaps]
        title = f"Refreshing {_names(snaps)}"
        ops.append(Operation(title, "update", [u.name for u in snaps],
                             [CommandStep(helper_argv("snap-refresh", names), title, privileged=True)],
                             source="Snap", app_keys=[k for u in snaps for k in u.app_keys],
                             update_keys=[u.key for u in snaps]))
    for installation in ("user", "system"):
        batch = [u for u in flatpaks if (u.installation or "system") == installation]
        if not batch:
            continue
        refs = [validate_flatpak_ref(u.package_name) for u in batch]
        title = f"Updating {_names(batch)}"
        if installation == "user":
            flatpak = shutil.which("flatpak") or "flatpak"
            step = CommandStep([flatpak, "update", "--user", "-y", "--noninteractive", *refs], title)
        else:
            step = CommandStep(helper_argv("flatpak-update", refs), title, privileged=True)
        ops.append(Operation(title, "update", [u.name for u in batch], [step], source="Flatpak",
                             app_keys=[k for u in batch for k in u.app_keys], update_keys=[u.key for u in batch]))
    return ops


def refresh_apt_lists_operation() -> Operation:
    title = "Refreshing APT package lists"
    return Operation(title, "refresh", ["APT package lists"],
                     [CommandStep(helper_argv("apt-update", []), title, privileged=True)], source="APT")


def remove_unused_flatpak_runtimes_operation(installation: str) -> Operation:
    flatpak = shutil.which("flatpak") or "flatpak"
    title = f"Removing unused Flatpak runtimes ({installation})"
    if installation == "user":
        step = CommandStep([flatpak, "uninstall", "--user", "--unused", "-y", "--noninteractive"], title)
    else:
        step = CommandStep(helper_argv("flatpak-remove-unused", []), title, privileged=True)
    return Operation(title, "cleanup", ["Unused Flatpak runtimes"], [step], source="Flatpak")


def _names(updates: list[UpdateInfo]) -> str:
    names = [u.name for u in updates]
    if len(names) == 1:
        return names[0]
    if len(names) <= 3:
        return ", ".join(names)
    return f"{len(names)} packages"
