"""Searching for and installing software (APT, Snap, Flatpak, local files).

Search uses AppStream metadata (APT DEP-11 and Flatpak remotes, the same data
GNOME Software uses) and the snapd store API. Installation always goes
through a preview + confirmation in the UI and through the privileged helper
for system changes. AppImages are installed per user without privileges.
"""

from __future__ import annotations

import configparser
import hashlib
import os
import re
import shutil
import stat
import subprocess
import threading
import urllib.parse
from dataclasses import dataclass, field

from gi.repository import GLib

from ..core.application_model import AppRecord, PackageManager
from ..core.safety import (
    FLATPAK_REF_RE,
    SNAP_NAME_RE,
    ValidationError,
    home_dir,
    validate_abs_path,
    validate_apt_package,
    validate_flatpak_ref,
    validate_flatpak_remote,
    validate_snap_name,
)
from ..package_managers.appimage import appimage_type, name_and_version
from ..package_managers.snap import SnapdClient, SnapdError
from .operation_service import CommandStep, Operation, PythonStep
from .privilege_service import helper_argv

_pool = None
_pool_lock = threading.Lock()


@dataclass
class Candidate:
    source: str                 # apt / snap / flatpak
    identifier: str             # apt package, snap name, or flatpak ref
    name: str
    summary: str = ""
    version: str = ""
    icon: str = ""
    origin: str = ""
    remote: str = ""            # flatpak remote
    classic: bool = False       # snap classic confinement
    installed: bool = False
    download_size: int | None = None
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.identifier}"


def _appstream_pool():
    global _pool
    with _pool_lock:
        if _pool is None:
            import gi

            gi.require_version("AppStream", "1.0")
            from gi.repository import AppStream

            pool = AppStream.Pool()
            pool.load(None)
            _pool = pool
        return _pool


def appstream_available() -> bool:
    try:
        import gi

        gi.require_version("AppStream", "1.0")
        from gi.repository import AppStream  # noqa: F401
        return True
    except (ImportError, ValueError):
        return False


def _icon_path(component) -> str:
    best = ""
    best_size = 0
    for icon in component.get_icons():
        filename = icon.get_filename() or ""
        if filename and os.path.isfile(filename):
            size = icon.get_width() or 0
            if size > best_size or not best:
                best, best_size = filename, size
    return best


def _flatpak_remote_from(component) -> str:
    for icon in component.get_icons():
        filename = icon.get_filename() or ""
        m = re.search(r"/flatpak/appstream/([^/]+)/", filename)
        if m:
            return m.group(1)
    return ""


def _installed_sets(records: list[AppRecord]) -> tuple[set[str], set[str], set[str]]:
    apt = {r.package_name for r in records if r.package_manager == PackageManager.APT}
    snaps = {r.package_name for r in records if r.package_manager == PackageManager.SNAP}
    flatpaks = {r.package_name for r in records if r.package_manager == PackageManager.FLATPAK}
    return apt, snaps, flatpaks


def _dpkg_installed(names: list[str]) -> set[str]:
    if not names or not shutil.which("dpkg-query"):
        return set()
    try:
        proc = subprocess.run(["dpkg-query", "-W", "-f=${Package} ${db:Status-Status}\\n", *names],
                              capture_output=True, text=True, timeout=20,
                              env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"})
    except (OSError, subprocess.SubprocessError):
        return set()
    return {line.split()[0] for line in proc.stdout.splitlines() if line.endswith(" installed")}


def search(query: str, sources: list[str], records: list[AppRecord], limit: int = 60):
    """Blocking search; run in a worker thread. Returns (candidates, errors)."""
    query = query.strip()
    results: list[Candidate] = []
    errors: dict[str, str] = {}
    if len(query) < 2:
        return results, errors
    apt_installed, snap_installed, flatpak_installed = _installed_sets(records)

    if {"apt", "flatpak"} & set(sources):
        try:
            pool = _appstream_pool()
            found = pool.search(query)
            components = found.as_array() if hasattr(found, "as_array") else list(found)
            seen: set[str] = set()
            apt_names = []
            for c in components:
                bundles = [b for b in c.get_bundles() if b.get_kind().value_nick == "flatpak"]
                if bundles and "flatpak" in sources:
                    ref = bundles[0].get_id()
                    short = ref.split("/", 1)[1] if ref.startswith("app/") else ref
                    if not FLATPAK_REF_RE.match(short) or ("flatpak", short) in seen:
                        continue
                    seen.add(("flatpak", short))
                    app_id = short.split("/")[0]
                    results.append(Candidate(
                        source="flatpak", identifier=short, name=c.get_name() or app_id,
                        summary=c.get_summary() or "", icon=_icon_path(c),
                        remote=_flatpak_remote_from(c), origin=_flatpak_remote_from(c) or "Flatpak",
                        installed=app_id in flatpak_installed, extra={"app_id": app_id}))
                elif not bundles and "apt" in sources:
                    pkg = c.get_pkgname()
                    if not pkg or ("apt", pkg) in seen:
                        continue
                    try:
                        validate_apt_package(pkg)
                    except ValidationError:
                        continue
                    seen.add(("apt", pkg))
                    apt_names.append(pkg)
                    results.append(Candidate(
                        source="apt", identifier=pkg, name=c.get_name() or pkg, summary=c.get_summary() or "",
                        icon=_icon_path(c), origin=c.get_origin() or "", installed=pkg in apt_installed))
                if len(results) >= limit:
                    break
            # Launcher-less packages are not in our inventory; ask dpkg.
            really = _dpkg_installed(apt_names)
            for cand in results:
                if cand.source == "apt" and cand.identifier in really:
                    cand.installed = True
        except Exception as exc:  # metadata missing or unreadable
            errors["appstream"] = f"Software catalogue unavailable: {exc}"

    if "snap" in sources:
        try:
            client = SnapdClient(timeout=20)
            found = client.get("/v2/find?" + urllib.parse.urlencode({"q": query})) or []
            for item in found[:25]:
                name = item.get("name", "")
                if not SNAP_NAME_RE.match(name):
                    continue
                publisher = item.get("publisher") or {}
                results.append(Candidate(
                    source="snap", identifier=name, name=item.get("title") or name,
                    summary=item.get("summary", ""), version=item.get("version", ""),
                    origin=(publisher.get("display-name") or "") + (
                        " ✓" if publisher.get("validation") in ("verified", "starred") else ""),
                    classic=item.get("confinement") == "classic", installed=name in snap_installed,
                    download_size=item.get("download-size")))
        except SnapdError as exc:
            errors["snap"] = str(exc)
    return results, errors


# ---------------------------------------------------------------------------
# Previews
# ---------------------------------------------------------------------------

def file_sha256(path: str) -> str:
    """Hash of the file the user reviews; the root helper installs only a file with this hash."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    digest = hashlib.sha256()
    with os.fdopen(fd, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def simulate_apt_install(target: str) -> tuple[list[str], list[str], str]:
    """(installed, removed, error) for a package name or a local .deb path (dry run).

    Installs never remove packages (the helper passes --no-remove), so a
    non-empty removal list means the installation cannot proceed.
    """
    if target.startswith("/"):
        validate_abs_path(target)
        if not target.endswith(".deb"):
            raise ValidationError("Not a .deb file")
    else:
        validate_apt_package(target)
    try:
        proc = subprocess.run(["apt-get", "-s", "-o", "Debug::NoLocking=1", "install", target],
                              capture_output=True, text=True, timeout=180,
                              env={"LC_ALL": "C.UTF-8", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"})
    except (OSError, subprocess.SubprocessError) as exc:
        return [], [], f"Could not run the APT simulation: {exc}"
    if proc.returncode != 0:
        errors = [l[2:].strip() for l in proc.stderr.splitlines() if l.startswith("E:")]
        return [], [], errors[-1] if errors else "APT cannot install this package."
    installed = [line.split()[1] for line in proc.stdout.splitlines() if line.startswith("Inst ")]
    removed = [line.split()[1] for line in proc.stdout.splitlines() if line.startswith(("Remv ", "Purg "))]
    return installed, removed, ""


def deb_info(path: str) -> dict[str, str]:
    """Read control fields from a .deb without installing or executing it."""
    validate_abs_path(path)
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode):
        raise ValidationError("Not a regular file")
    fields = ["Package", "Version", "Architecture", "Maintainer", "Description", "Homepage"]
    try:
        proc = subprocess.run(["dpkg-deb", "-f", path, *fields], capture_output=True, text=True,
                              timeout=30, env={"LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"})
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValidationError(f"Cannot read the package: {exc}") from exc
    if proc.returncode != 0:
        raise ValidationError("The file is not a valid Debian package.")
    info: dict[str, str] = {}
    key = None
    for line in proc.stdout.splitlines():
        if line[:1] in (" ", "\t") and key:
            info[key] += "\n" + line.strip()
            continue
        key, _, value = line.partition(":")
        info[key] = value.strip()
    info["SHA256"] = file_sha256(path)
    return info


def flatpakref_info(path: str) -> dict[str, str]:
    validate_abs_path(path)
    if os.path.getsize(path) > 256 * 1024:
        raise ValidationError("The .flatpakref file is too large")
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with open(path, encoding="utf-8") as fh:
            parser.read_file(fh)
    except (OSError, configparser.Error, UnicodeDecodeError) as exc:
        raise ValidationError(f"Invalid .flatpakref file: {exc}") from exc
    if "Flatpak Ref" not in parser:
        raise ValidationError("Invalid .flatpakref file: missing [Flatpak Ref] section")
    section = parser["Flatpak Ref"]
    name = section.get("Name", "")
    if not FLATPAK_REF_RE.match(name):
        raise ValidationError("Invalid application ID in .flatpakref")
    for key in ("Url", "RuntimeRepo"):
        value = section.get(key, "")
        if value and not value.startswith("https://"):
            raise ValidationError(f"The .flatpakref {key} does not use https")
    if not section.get("Url"):
        raise ValidationError("The .flatpakref file has no repository URL")
    return {"name": name, "title": section.get("Title", name), "url": section.get("Url", ""),
            "branch": section.get("Branch", "stable"), "runtime_repo": section.get("RuntimeRepo", ""),
            "sha256": file_sha256(path)}


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

def install_operation(candidate: Candidate) -> Operation:
    title = f"Installing {candidate.name}"
    if candidate.source == "apt":
        validate_apt_package(candidate.identifier)
        step = CommandStep(helper_argv("apt-install", [candidate.identifier]), title, privileged=True)
        source = "APT"
    elif candidate.source == "snap":
        validate_snap_name(candidate.identifier)
        args = (["--classic"] if candidate.classic else []) + [candidate.identifier]
        step = CommandStep(helper_argv("snap-install", args), title, privileged=True)
        source = "Snap"
    elif candidate.source == "flatpak":
        validate_flatpak_ref(candidate.identifier)
        remote = candidate.remote or flatpak_remote_for(candidate.extra.get("app_id", ""))
        validate_flatpak_remote(remote)
        step = CommandStep(helper_argv("flatpak-install", [remote, candidate.identifier]), title, privileged=True)
        source = "Flatpak"
    else:
        raise ValueError(f"Unsupported source {candidate.source}")
    return Operation(title, "install", [candidate.name], [step], source=source)


def flatpak_remote_for(app_id: str) -> str:
    """Find a configured remote that provides ``app_id``."""
    flatpak = shutil.which("flatpak")
    if not flatpak or not app_id:
        return ""
    try:
        proc = subprocess.run([flatpak, "search", "--columns=application,remotes", app_id], capture_output=True,
                              text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0] == app_id:
            return parts[1].split(",")[0].strip()
    return ""


def deb_install_operation(path: str, name: str, sha256: str) -> Operation:
    title = f"Installing {name}"
    step = CommandStep(helper_argv("apt-install-deb", [f"--sha256={sha256}", path]), title, privileged=True)
    return Operation(title, "install", [name], [step], source="APT")


def flatpakref_install_operation(path: str, title_name: str, sha256: str) -> Operation:
    title = f"Installing {title_name}"
    step = CommandStep(helper_argv("flatpak-install-ref", [f"--sha256={sha256}", path]), title, privileged=True)
    return Operation(title, "install", [title_name], [step], source="Flatpak")


def _desktop_exec_quote(path: str) -> str:
    """Quote a path for the Exec key (Desktop Entry spec, 'Exec key' section)."""
    escaped = path.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
    return f'"{escaped}"'


def appimage_install_plan(source_path: str) -> dict[str, str]:
    """Where an AppImage would be installed (no changes made)."""
    validate_abs_path(source_path)
    if not appimage_type(source_path):
        raise ValidationError("The file is not an AppImage (magic bytes not found)")
    home = home_dir()
    apps_dir = os.path.join(home, "Applications")
    base = os.path.basename(source_path)
    target = source_path if os.path.dirname(source_path) == apps_dir else os.path.join(apps_dir, base)
    name, version = name_and_version(base)
    stem = re.sub(r"[^A-Za-z0-9_.-]", "-", name.lower()).strip("-") or "appimage"
    apps = os.path.join(home, ".local", "share", "applications")
    launcher = os.path.join(apps, f"hangar-appimage-{stem}.desktop")
    legacy = os.path.join(apps, f"uam-appimage-{stem}.desktop")  # name used by 1.0.1 and older
    if os.path.lexists(legacy) and not os.path.lexists(launcher):
        launcher = legacy
    return {"source": source_path, "target": target, "launcher": launcher, "name": name, "version": version}


def appimage_install_operation(plan: dict[str, str]) -> Operation:
    def run(log):
        source, target, launcher = plan["source"], plan["target"], plan["launcher"]
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if source != target:
            if os.path.lexists(target):
                raise RuntimeError(f"{target} already exists; not overwriting it.")
            shutil.copy2(source, target, follow_symlinks=False)
            log(f"Copied to {target}")
        mode = os.lstat(target).st_mode
        os.chmod(target, mode | stat.S_IXUSR | stat.S_IRUSR)
        log("Marked as executable")
        os.makedirs(os.path.dirname(launcher), exist_ok=True)
        if os.path.lexists(launcher):
            raise RuntimeError(f"{launcher} already exists; not overwriting it.")
        kf = GLib.KeyFile()
        group = "Desktop Entry"
        kf.set_string(group, "Type", "Application")
        kf.set_string(group, "Name", plan["name"])
        kf.set_string(group, "Comment", "AppImage application")
        kf.set_string(group, "Exec", f"{_desktop_exec_quote(target)} %U")
        kf.set_string(group, "TryExec", target)
        kf.set_string(group, "Icon", "application-x-executable")
        kf.set_string(group, "Terminal", "false")
        kf.set_string(group, "Categories", "Utility;")
        kf.set_string(group, "X-Hangar-Installed", "true")
        if plan.get("version"):
            kf.set_string(group, "X-AppImage-Version", plan["version"])
        kf.save_to_file(launcher)
        log(f"Created launcher {launcher}")

    title = f"Installing {plan['name']}"
    return Operation(title, "install", [plan["name"]], [PythonStep(run, title)], source="AppImage")
