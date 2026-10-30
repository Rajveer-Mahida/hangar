"""Source-specific uninstall planning and execution.

``build_plan`` never changes anything: it inspects the system (including an
``apt-get --simulate`` dry run) and describes exactly what would be removed.
``operations_for`` turns a reviewed plan into queued operations.

Rules:
  * package-managed software is removed with its package manager;
  * user files are moved to the Trash (recoverable), never deleted outright;
  * system files go through the privileged helper, which re-checks the policy;
  * anything that fails the safety policy is shown but cannot be selected.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field

from gi.repository import Gio, GLib

from ..core.application_model import AppRecord, PackageManager, Source, format_size
from ..core.safety import (
    ValidationError,
    home_dir,
    is_icon_file,
    removal_problem,
    validate_apt_package,
    validate_flatpak_ref,
    validate_snap_name,
)
from .operation_service import CommandStep, Operation, PythonStep
from .privilege_service import PrivilegeError, helper_argv

DESKTOP_META_RE = re.compile(r"^(ubuntu|kubuntu|xubuntu|lubuntu|zorin-os|pop|elementary|mint)-desktop.*$|"
                             r"^(gnome-shell|gdm3|xorg|systemd|network-manager|sudo|apt|dpkg|snapd|"
                             r"flatpak|ubuntu-minimal|ubuntu-standard|linux-image-.*|linux-generic.*)$")
SNAP_SYSTEM_TYPES = {"base", "os", "snapd", "kernel", "gadget", "core"}


@dataclass
class PlanItem:
    kind: str                 # package / snap / flatpak / file / directory / launcher / link / icon / data
    target: str               # package name or absolute path
    description: str = ""
    size: int | None = None
    requires_root: bool = False
    selected: bool = True
    optional: bool = False
    blocked: str = ""         # reason this item cannot be removed

    @property
    def removable(self) -> bool:
        return not self.blocked


@dataclass
class UninstallPlan:
    record: AppRecord
    method: str               # apt / snap / flatpak / files / unsupported
    items: list[PlanItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    affected_packages: list[str] = field(default_factory=list)
    installed_by_removal: list[str] = field(default_factory=list)
    unused_after: list[str] = field(default_factory=list)
    vendor_uninstaller: str = ""
    purge: bool = False                   # apt purge / snap --purge
    supports_purge: bool = False
    purge_label: str = ""
    requires_root: bool = False

    @property
    def supported(self) -> bool:
        return self.method != "unsupported" and not self.blockers

    def selected_items(self) -> list[PlanItem]:
        return [i for i in self.items if i.selected and i.removable]

    @property
    def total_size(self) -> int:
        return sum(i.size or 0 for i in self.selected_items())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def directory_size(path: str, limit_files: int = 200000) -> int | None:
    """Disk usage of a tree without following symlinks (bounded)."""
    total = 0
    count = 0
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not os.path.isdir(path) or os.path.islink(path):
        return st.st_size
    for root, dirs, files in os.walk(path, followlinks=False):
        for name in files + [d for d in dirs if os.path.islink(os.path.join(root, d))]:
            count += 1
            if count > limit_files:
                return None
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                continue
    return total


def _dpkg_owner(path: str) -> str | None:
    """Owner lookup for system paths (dpkg-query, argument array)."""
    if path.startswith(home_dir() + "/") or not shutil.which("dpkg-query"):
        return None
    try:
        proc = subprocess.run(["dpkg-query", "-S", "--", path], capture_output=True, text=True, timeout=15,
                              env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"})
    except (OSError, subprocess.SubprocessError):
        return None
    for line in proc.stdout.splitlines():
        if line.startswith("diversion by"):
            continue
        pkgs, _, owned = line.partition(": ")
        owned = owned.strip()
        if owned == path or owned.startswith(path.rstrip("/") + "/"):
            return pkgs.split(",")[0].strip()
    return None


def simulate_apt_removal(packages: list[str], purge: bool = False,
                         autoremove: bool = False) -> tuple[list[str], list[str], str]:
    """Return (removed, installed, error) from an unprivileged apt-get dry run.

    Removing a package can make APT install a replacement (for example an
    alternative sound server), so installs are reported too.
    """
    for p in packages:
        validate_apt_package(p)
    cmd = ["apt-get", "-s", "-o", "Debug::NoLocking=1", "purge" if purge else "remove"]
    if autoremove:
        cmd.append("--autoremove")
    cmd += packages
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                              env={"LC_ALL": "C.UTF-8", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"})
    except (OSError, subprocess.SubprocessError) as exc:
        return [], [], f"Could not run the APT simulation: {exc}"
    if proc.returncode != 0:
        errors = [l[2:].strip() for l in proc.stderr.splitlines() if l.startswith("E:")]
        return [], [], errors[-1] if errors else "APT could not compute the removal."
    removed, installed = [], []
    for line in proc.stdout.splitlines():
        if line.startswith(("Remv ", "Purg ")):
            removed.append(line.split()[1])
        elif line.startswith("Inst "):
            installed.append(line.split()[1])
    return removed, installed, ""


def _related_user_data(record: AppRecord) -> list[str]:
    """Per-user configuration/data folders that probably belong to an app."""
    home = home_dir()
    names = set()
    for candidate in (record.name, record.package_name, os.path.basename(record.exec_path or "")):
        candidate = re.sub(r"\.(appimage|AppImage|desktop)$", "", candidate or "")
        candidate = re.sub(r"[-_ .]?v?\d+(\.\d+)+.*$", "", candidate)
        if len(candidate) >= 3:
            names.add(candidate)
            names.add(candidate.lower())
            names.add(candidate.lower().replace(" ", "-"))
            names.add(candidate.replace(" ", ""))
    found = []
    for base in (".config", ".local/share", ".cache", ".local/state"):
        for name in sorted(names):
            path = os.path.join(home, base, name)
            if os.path.isdir(path) and not os.path.islink(path) and path not in found:
                found.append(path)
    return found


def _icon_used_elsewhere(icon_path: str, record: AppRecord, all_records: list[AppRecord]) -> bool:
    stem = os.path.splitext(os.path.basename(icon_path))[0]
    for other in all_records:
        if other.key == record.key:
            continue
        if other.icon in (icon_path, stem):
            return True
    return False


def _path_item(path: str, kind: str, description: str, record: AppRecord, owner_lookup=None,
               selected: bool = True, optional: bool = False) -> PlanItem:
    home = home_dir()
    blocked = removal_problem(path, home, owner_lookup) or ""
    size = None
    if not blocked:
        size = directory_size(path) if kind in ("directory", "data") else _lsize(path)
    return PlanItem(kind=kind, target=path, description=description, size=size,
                    requires_root=not path.startswith(home + "/"), selected=selected and not blocked,
                    optional=optional, blocked=blocked)


def _lsize(path: str) -> int | None:
    try:
        return os.lstat(path).st_size
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Plan builders
# ---------------------------------------------------------------------------

def build_plan(record: AppRecord, all_records: list[AppRecord] | None = None, purge: bool = False) -> UninstallPlan:
    """Inspect the system and describe the removal of ``record``. Read-only."""
    all_records = all_records or []
    pm = record.package_manager
    if pm == PackageManager.APT:
        return _plan_apt(record, all_records, purge)
    if pm == PackageManager.SNAP:
        return _plan_snap(record, purge)
    if pm == PackageManager.FLATPAK:
        return _plan_flatpak(record)
    return _plan_files(record, all_records)


def _plan_apt(record: AppRecord, all_records: list[AppRecord], purge: bool) -> UninstallPlan:
    plan = UninstallPlan(record=record, method="apt", supports_purge=True, purge=purge, requires_root=True,
                         purge_label="Also remove system-wide configuration files (purge)")
    pkg = record.package_name
    try:
        validate_apt_package(pkg)
    except ValidationError as exc:
        plan.method = "unsupported"
        plan.blockers.append(str(exc))
        return plan
    removed, installed, error = simulate_apt_removal([pkg], purge)
    if error:
        plan.blockers.append(error)
        return plan
    if not removed:
        plan.blockers.append(f"APT reports that '{pkg}' would not be removed (is it still installed?).")
        return plan
    plan.affected_packages = removed
    plan.installed_by_removal = installed
    if installed:
        plan.warnings.append("To keep dependencies satisfied, APT would also install: " + ", ".join(installed[:12])
                             + (" …" if len(installed) > 12 else ""))
    for name in removed:
        desc = "Selected application package" if name == pkg else "Depends on the package being removed"
        plan.items.append(PlanItem(kind="package", target=name, description=desc, requires_root=True,
                                   size=record.size if name == pkg else None))
    others = [p for p in removed if p != pkg]
    if others:
        plan.warnings.append(
            f"Removing {pkg} also removes {len(others)} other package(s) that depend on it: "
            + ", ".join(others[:12]) + (" …" if len(others) > 12 else ""))
    critical = [p for p in removed if DESKTOP_META_RE.match(p.split(":")[0])]
    if critical:
        plan.warnings.insert(0, "Warning: this also removes core system or desktop packages ("
                             + ", ".join(critical) + "). This can break your desktop session.")
    with_autoremove, _, _ = simulate_apt_removal([pkg], purge, autoremove=True)
    plan.unused_after = sorted(set(with_autoremove) - set(removed))
    if plan.unused_after:
        plan.info.append(f"{len(plan.unused_after)} package(s) are no longer needed afterwards. They are kept; "
                         "you can review them later with 'apt autoremove'.")
    launchers = [r for r in all_records if r.package_manager == PackageManager.APT and r.package_name in removed
                 and r.key != record.key and not r.hidden]
    if launchers:
        plan.info.append("These applications are part of the removed packages and disappear too: "
                         + ", ".join(sorted({r.name for r in launchers})))
    plan.info.append("Your personal settings in your home folder are not removed.")
    return plan


def _plan_snap(record: AppRecord, purge: bool) -> UninstallPlan:
    plan = UninstallPlan(record=record, method="snap", supports_purge=True, purge=purge, requires_root=True,
                         purge_label="Do not keep a snapshot of the app's data (snap --purge)")
    name = record.package_name
    try:
        validate_snap_name(name)
    except ValidationError as exc:
        plan.method = "unsupported"
        plan.blockers.append(str(exc))
        return plan
    if record.extra.get("type") in SNAP_SYSTEM_TYPES:
        plan.blockers.append(f"'{name}' is a {record.extra.get('type')} snap required by the system and other snaps.")
        return plan
    plan.items.append(PlanItem(kind="snap", target=name, description=f"Snap package (revision "
                               f"{record.extra.get('revision', '?')}, {record.install_path})",
                               size=record.size, requires_root=True))
    plan.info.append("snapd removes the app's data in ~/snap/" + name + ". Unless purge is selected, it keeps "
                     "an automatic snapshot of that data for 31 days.")
    if record.is_component:
        plan.warnings.append("This snap looks like a shared runtime or content provider. Other snaps may need it; "
                             "snapd refuses the removal if they do.")
    return plan


def _plan_flatpak(record: AppRecord) -> UninstallPlan:
    installation = record.extra.get("installation", "system")
    ref = record.extra.get("ref", "")
    plan = UninstallPlan(record=record, method="flatpak", requires_root=installation == "system")
    try:
        validate_flatpak_ref(ref)
    except ValidationError as exc:
        plan.method = "unsupported"
        plan.blockers.append(str(exc))
        return plan
    if installation not in ("system", "user"):
        plan.method = "unsupported"
        plan.blockers.append(f"Custom Flatpak installation '{installation}' is not supported.")
        return plan
    plan.items.append(PlanItem(kind="flatpak", target=ref, description=f"Flatpak ({installation} installation)",
                               size=record.size, requires_root=installation == "system"))
    data_dir = record.extra.get("data_dir", "")
    if data_dir and os.path.isdir(data_dir) and not os.path.islink(data_dir):
        item = _path_item(data_dir, "data", "Your app data and settings (moved to Trash)", record,
                          selected=False, optional=True)
        item.requires_root = False
        plan.items.append(item)
    if record.is_component:
        plan.warnings.append("This is a runtime. Flatpak refuses to remove runtimes that installed apps still use.")
    plan.info.append("Runtimes the app used are kept. Unused runtimes can be removed from the Package Managers page.")
    return plan


def _plan_files(record: AppRecord, all_records: list[AppRecord]) -> UninstallPlan:
    plan = UninstallPlan(record=record, method="files")
    home = home_dir()
    owner_lookup = _dpkg_owner
    seen: set[str] = set()

    def add(item: PlanItem) -> None:
        if item.target in seen:
            return
        seen.add(item.target)
        plan.items.append(item)

    extra = record.extra
    root = record.install_path
    if record.source == Source.APPIMAGE:
        add(_path_item(root, "file", "AppImage file", record, owner_lookup))
    elif extra.get("web_app") or extra.get("custom_launcher") or extra.get("broken_launcher"):
        pass  # only launchers below
    elif extra.get("broken_link"):
        add(_path_item(root, "link", "Broken symbolic link", record, owner_lookup))
    elif root:
        is_dir = os.path.isdir(root) and not os.path.islink(root)
        guessed = bool(extra.get("root_from_script")) and is_dir and root.startswith(home + "/")
        description = "Installation folder" if is_dir else "Program file"
        if guessed:
            description = "Folder guessed from a script launcher — check its contents before selecting"
        add(_path_item(root, "directory" if is_dir else "file", description, record, owner_lookup,
                       selected=not guessed))

    for launcher in extra.get("launchers", []) or ([record.desktop_file] if record.desktop_file else []):
        add(_path_item(launcher, "launcher", "Desktop launcher", record, owner_lookup))
    for link in extra.get("symlinks", []):
        add(_path_item(link, "link", "Command link", record, owner_lookup))
    for related in record.related_paths:
        if related in seen:
            continue
        if related.endswith(".desktop"):
            add(_path_item(related, "launcher", "Desktop launcher", record, owner_lookup))
        elif os.path.islink(related):
            add(_path_item(related, "link", "Command link", record, owner_lookup))
        elif is_icon_file(related):
            used = _icon_used_elsewhere(related, record, all_records)
            item = _path_item(related, "icon", "Launcher icon" + (" (also used by another app)" if used else ""),
                              record, owner_lookup, selected=not used)
            add(item)
        # Anything else (e.g. an Icon= value naming a folder) is never offered for removal.

    if not (extra.get("web_app") or extra.get("custom_launcher") or extra.get("broken_link")):
        for data in _related_user_data(record):
            add(_path_item(data, "data", "Possible user settings or data (check before removing)", record,
                           selected=False, optional=True))

    # Other installations that live inside the same folder
    if root and os.path.isdir(root):
        prefix = root.rstrip("/") + "/"
        sharing = [r.name for r in all_records if r.key != record.key and (
            (r.exec_path or "").startswith(prefix) or (r.install_path or "").startswith(prefix))]
        if sharing:
            plan.warnings.append("Other detected software lives inside this folder and would be removed too: "
                                 + ", ".join(sorted(set(sharing))))

    uninstaller = extra.get("vendor_uninstaller", "")
    if uninstaller:
        plan.vendor_uninstaller = uninstaller
        plan.warnings.append(f"The vendor provides an uninstaller ({uninstaller}). Using it is recommended because it "
                             "may also remove services, drivers or system files this app cannot know about.")
    if record.confidence.value == "unknown":
        plan.warnings.append("This item was found by scanning and could not be identified with certainty. "
                             "Review every path before removing it.")
    blocked = [i for i in plan.items if i.blocked and not i.optional]
    if blocked:
        plan.warnings.append(f"{len(blocked)} item(s) cannot be removed safely and will be left untouched.")
    if not any(i.removable for i in plan.items):
        plan.blockers.append("Nothing here can be removed safely by this app.")
    if root and not root.startswith(home + "/"):
        similar = _similar_packages(os.path.basename(root))
        if similar:
            plan.warnings.append("Installed packages with a similar name exist (" + ", ".join(similar[:4]) +
                                 "). This folder may be created or used by them at runtime.")
    plan.requires_root = any(i.requires_root for i in plan.selected_items())
    if any(not i.requires_root and i.removable for i in plan.items):
        plan.info.append("Files in your home folder are moved to the Trash, so they can be restored.")
    if any(i.requires_root and i.removable for i in plan.items):
        plan.info.append("System files require administrator authentication and are deleted permanently.")
    return plan


def _similar_packages(name: str) -> list[str]:
    """Installed package names that start with ``name`` (e.g. containerd -> containerd.io)."""
    key = re.sub(r"[^a-z0-9]", "", name.lower())
    if len(key) < 4 or not shutil.which("dpkg-query"):
        return []
    try:
        proc = subprocess.run(["dpkg-query", "-W", "-f=${binary:Package}\n"], capture_output=True, text=True,
                              timeout=15, env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"})
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for pkg in proc.stdout.split():
        norm = re.sub(r"[^a-z0-9]", "", pkg.split(":")[0].lower())
        if norm.startswith(key):
            found.append(pkg)
    return found


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def trash_paths(paths: list[str], log) -> None:
    """Move user files to the Trash after re-validating each path."""
    home = home_dir()
    failures = []
    for path in paths:
        problem = removal_problem(path, home)
        if problem:
            failures.append(problem)
            log(f"Skipped {path}: {problem}")
            continue
        try:
            Gio.File.new_for_path(path).trash(None)
            log(f"Moved to Trash: {path}")
        except GLib.Error as exc:
            failures.append(f"{path}: {exc.message}")
            log(f"Could not move {path} to the Trash: {exc.message}")
    if failures:
        raise RuntimeError("Some files could not be moved to the Trash: " + "; ".join(failures[:3]))


def operations_for(plan: UninstallPlan) -> list[Operation]:
    """Turn a reviewed plan into queued operations. Raises PrivilegeError/ValueError."""
    if not plan.supported:
        raise ValueError("This removal is not supported: " + "; ".join(plan.blockers))
    r = plan.record
    steps = []
    if plan.method == "apt":
        args = (["--purge"] if plan.purge else []) + [
            f"--expect={','.join(sorted(plan.affected_packages))}",
            f"--expect-install={','.join(sorted(plan.installed_by_removal))}",
            r.package_name]
        steps.append(CommandStep(helper_argv("apt-remove", args), f"Removing {r.package_name} with APT",
                                 privileged=True))
    elif plan.method == "snap":
        args = (["--purge"] if plan.purge else []) + [r.package_name]
        steps.append(CommandStep(helper_argv("snap-remove", args), f"Removing snap {r.package_name}",
                                 privileged=True))
    elif plan.method == "flatpak":
        ref = r.extra.get("ref", "")
        validate_flatpak_ref(ref)
        if r.extra.get("installation") == "user":
            flatpak = shutil.which("flatpak") or "flatpak"
            steps.append(CommandStep([flatpak, "uninstall", "--user", "-y", "--noninteractive", ref],
                                     f"Removing {ref}"))
        else:
            steps.append(CommandStep(helper_argv("flatpak-uninstall", [ref]), f"Removing {ref}", privileged=True))
        data = [i.target for i in plan.selected_items() if i.kind == "data"]
        if data:
            steps.append(PythonStep(lambda log, d=data: trash_paths(d, log), "Moving app data to the Trash"))
    elif plan.method == "files":
        selected = plan.selected_items()
        user_paths = [i.target for i in selected if not i.requires_root]
        system_paths = [i.target for i in selected if i.requires_root]
        if not user_paths and not system_paths:
            raise ValueError("Nothing is selected for removal.")
        if system_paths:
            steps.append(CommandStep(helper_argv("remove-paths", system_paths), "Removing system files",
                                     privileged=True))
        if user_paths:
            steps.append(PythonStep(lambda log, p=user_paths: trash_paths(p, log), "Moving files to the Trash"))
    else:
        raise ValueError("Unsupported removal method")
    title = f"Uninstalling {r.name}"
    return [Operation(title, "remove", [r.name], steps, source=r.source.label, app_keys=[r.key])]


def describe_plan(plan: UninstallPlan) -> str:
    lines = [f"{plan.record.name} ({plan.record.source.label})"]
    for item in plan.items:
        mark = "x" if item.selected and item.removable else " "
        size = f" ({format_size(item.size)})" if item.size else ""
        blocked = f" — blocked: {item.blocked}" if item.blocked else ""
        lines.append(f"[{mark}] {item.kind}: {item.target}{size}{blocked}")
    return "\n".join(lines)


__all__ = ["PlanItem", "UninstallPlan", "build_plan", "operations_for", "simulate_apt_removal", "trash_paths",
           "describe_plan", "PrivilegeError"]
