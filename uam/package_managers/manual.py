"""Discovery of manually installed and vendor-installed software.

Signals used, strongest first:
  * desktop launchers that no package manager owns, and what they launch
  * directories in /opt (and user-configured folders) not owned by dpkg
  * executables in /usr/local/bin (and configured bin folders)
  * symlinks in bin folders that point into an installation directory

Every executable is *not* assumed to be an application: items without a
launcher or other metadata are reported with ``Confidence.UNKNOWN`` so the
user can review them. Nothing is executed or modified while scanning.
"""

from __future__ import annotations

import json
import os
import re
import stat

from ..core.application_model import AppRecord, Confidence, PackageManager, Source
from ..core.desktop_entry import DesktopEntry
from ..core.safety import is_icon_file, protected_paths
from .base import Provider, ScanContext, shown_in_current_desktop

SYSTEM_BIN_DIRS = ("/usr/local/bin", "/usr/local/sbin")
_UNINSTALLER_RE = re.compile(r"^(uninstall|uninst|remove|uninstaller)([-_.][\w.-]*)?$", re.I)
_HELPER_NAMES = re.compile(
    r"(chrome[-_]sandbox|crashpad|crash[-_]?handler|update[r]?|uninstall|helper|\.so(\.\d+)*$|"
    r"\.(txt|md|json|xml|png|svg|pak|dat|bin|ini|cfg|conf|desktop)$)", re.I)
_BROWSER_HINT = re.compile(r"(chrome|chromium|brave|edge|vivaldi|opera|epiphany|firefox)", re.I)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def file_kind(path: str) -> str:
    """'elf', 'script', or '' for an executable regular file (no symlink follow)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return ""
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or not st.st_mode & 0o111:
            return ""
        head = os.read(fd, 4)
    except OSError:
        return ""
    finally:
        os.close(fd)
    if head == b"\x7fELF":
        return "elf"
    if head[:2] == b"#!":
        return "script"
    return ""


def boundaries(home: str) -> set[str]:
    """Directories that are never treated as an application's install root."""
    result = set(protected_paths(home))
    result.update({
        home, os.path.join(home, ".local"), os.path.join(home, ".local", "share"),
        os.path.join(home, ".local", "bin"), os.path.join(home, ".local", "lib"),
        os.path.join(home, "Downloads"), os.path.join(home, "Applications"),
        os.path.join(home, "Desktop"), os.path.join(home, "bin"), os.path.join(home, "opt"),
        os.path.join(home, "Apps"), os.path.join(home, "AppImages"), os.path.join(home, "Documents"),
        os.path.join(home, ".config"), os.path.join(home, ".cache"),
        "/opt", "/usr/local", "/usr/local/bin", "/usr/local/sbin", "/usr/local/lib", "/usr/local/share",
    })
    return result


def install_root(target: str, home: str, extra_roots: tuple[str, ...] = ()) -> str:
    """Best guess of the directory an application was installed into."""
    target = os.path.normpath(target)
    if target.startswith("/opt/"):
        return "/opt/" + target[5:].split("/", 1)[0]
    for root in extra_roots:
        root = os.path.normpath(root)
        if target.startswith(root + "/"):
            return os.path.join(root, target[len(root) + 1:].split("/", 1)[0])
    stop = boundaries(home)
    directory = os.path.dirname(target)
    if os.path.basename(directory) in ("bin", "sbin", "MacOS") and os.path.dirname(directory) not in stop:
        directory = os.path.dirname(directory)
    if directory in stop or directory == os.path.dirname(home):
        return target
    return directory


def _find_uninstaller(root: str) -> str:
    if not os.path.isdir(root):
        return ""
    for depth_dir in (root, os.path.join(root, "bin")):
        try:
            with os.scandir(depth_dir) as it:
                for item in it:
                    if _UNINSTALLER_RE.match(item.name) and item.is_file(follow_symlinks=False):
                        if os.access(item.path, os.X_OK) or item.name.endswith(".sh"):
                            return item.path
        except OSError:
            continue
    return ""


def _main_executable(root: str, hint: str) -> str:
    """Pick the most plausible main executable inside an install directory."""
    if not os.path.isdir(root):
        return root if file_kind(root) else ""
    want = _norm(hint)
    best: tuple[int, str] | None = None
    stack = [(root, 0)]
    visited = 0
    while stack and visited < 400:
        directory, depth = stack.pop()
        try:
            with os.scandir(directory) as it:
                items = list(it)
        except OSError:
            continue
        for item in items:
            visited += 1
            try:
                if item.is_dir(follow_symlinks=False):
                    if depth < 2 and not item.name.startswith(".") and item.name not in (
                            "lib", "lib64", "locales", "resources", "share", "include", "doc", "plugins"):
                        stack.append((item.path, depth + 1))
                    continue
            except OSError:
                continue
            if _HELPER_NAMES.search(item.name):
                continue
            kind = file_kind(item.path)
            if not kind:
                continue
            score = (10 if _norm(item.name) == want else 4 if want and want in _norm(item.name) else 0)
            score += 3 if kind == "elf" else 1
            score -= depth
            if best is None or score > best[0]:
                best = (score, item.path)
    return best[1] if best else ""


def _read_version(root: str, dir_name: str) -> str:
    if os.path.isdir(root):
        for name in ("VERSION", "version", "version.txt", "VERSION.txt"):
            path = os.path.join(root, name)
            try:
                if os.path.isfile(path) and os.path.getsize(path) < 256:
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        text = fh.read().strip().splitlines()
                        if text and len(text[0]) < 40:
                            return text[0]
            except OSError:
                continue
        for rel in ("resources/app/package.json", "package.json"):
            path = os.path.join(root, rel)
            try:
                if os.path.isfile(path) and os.path.getsize(path) < 256 * 1024:
                    with open(path, encoding="utf-8") as fh:
                        version = json.load(fh).get("version", "")
                        if isinstance(version, str) and version:
                            return version
            except (OSError, ValueError, AttributeError):
                continue
    m = re.search(r"(\d+\.\d+(?:\.\d+)*)", dir_name)
    return m.group(1) if m else ""


def _pretty_name(path: str) -> str:
    base = os.path.basename(path.rstrip("/"))
    base = re.sub(r"\.(app|dir|d)$", "", base)
    base = re.sub(r"[-_.]?v?\d+(\.\d+)+.*$", "", base) or base
    words = re.split(r"[-_]+", base)
    return " ".join(w if any(c.isupper() for c in w) else w.capitalize() for w in words if w) or base


def _icon_file(icon: str, home: str) -> str:
    if not icon:
        return ""
    if os.path.isabs(icon):
        return icon if is_icon_file(icon) else ""
    for base in (os.path.join(home, ".local", "share", "icons", "hicolor"), "/usr/local/share/icons/hicolor"):
        for size in ("scalable", "512x512", "256x256", "128x128", "64x64", "48x48"):
            for ext in (".svg", ".png"):
                candidate = os.path.join(base, size, "apps", icon + ext)
                if is_icon_file(candidate):
                    return candidate
    return ""


class _Candidate:
    """Accumulates evidence for one install root during a scan."""

    def __init__(self, root: str):
        self.root = root
        self.launchers: list[DesktopEntry] = []
        self.symlinks: list[str] = []
        self.executable = ""
        self.from_system_launcher = False
        self.from_scan = ""  # which scan location found it


class ManualProvider(Provider):
    id = "manual"
    label = "Manual"
    icon_name = "folder-symbolic"
    description = "Software installed without a package manager (/opt, /usr/local, custom folders, launchers)"

    def available(self) -> bool:
        return True

    def tool_version(self) -> str:
        return "Built-in scanner"

    def __init__(self, opt_dir: str = "/opt", bin_dirs: tuple[str, ...] = SYSTEM_BIN_DIRS):
        self.opt_dir = opt_dir
        self.bin_dirs = bin_dirs

    def scan(self, ctx: ScanContext) -> list[AppRecord]:
        home = ctx.home
        dpkg = ctx.dpkg
        owner = (lambda p: dpkg.owner(p)) if dpkg else (lambda p: None)
        owner_below = (lambda p: dpkg.owner_below(p)) if dpkg else (lambda p: None)
        extra_roots = tuple(os.path.normpath(d) for d in ctx.manual_dirs)
        by_package: dict[str, list[AppRecord]] = {}
        for r in ctx.records:
            if r.package_manager == PackageManager.APT:
                by_package.setdefault(r.package_name, []).append(r)

        candidates: dict[str, _Candidate] = {}
        records: list[AppRecord] = []
        user_apps = os.path.join(home, ".local", "share", "applications")

        def candidate(root: str) -> _Candidate:
            if root not in candidates:
                candidates[root] = _Candidate(root)
            return candidates[root]

        # A. Launchers no package manager claimed --------------------------------
        for entry in ctx.desktop_index.entries:
            if ctx.is_claimed(entry.path):
                continue
            if "/snapd/desktop/" in entry.path or "/flatpak/exports/" in entry.path:
                continue
            if owner(entry.path):
                continue
            in_user_dir = entry.path.startswith(user_apps + os.sep)
            if entry.desktop_id in ctx.managed_desktop_ids and in_user_dir:
                continue  # user override of a packaged launcher
            target = entry.exec_target
            if entry.flatpak_id or entry.snap_instance or target.startswith("/snap/") or \
                    os.path.basename(target) == "flatpak":
                continue
            if target and ctx.is_claimed(target):
                continue

            if entry.web_app_id:
                records.append(self._web_app(entry, home))
                ctx.claim(entry.path)
                continue

            if not target or not os.path.exists(target):
                records.append(self._broken_launcher(entry, home))
                ctx.claim(entry.path)
                continue

            pkg = owner(target)
            if pkg:
                owners = by_package.get(pkg)
                if owners and not entry.visible:
                    for r in owners:
                        r.related_paths.append(entry.path)
                    ctx.claim(entry.path)
                    continue
                records.append(self._custom_launcher(entry, pkg, home))
                ctx.claim(entry.path)
                continue

            root = install_root(target, home, extra_roots)
            if owner_below(root) and root != target:
                root = target
            cand = candidate(root)
            cand.launchers.append(entry)
            cand.executable = cand.executable or target
            if not in_user_dir:
                cand.from_system_launcher = True
            ctx.claim(entry.path)

        # B. Unowned directories in /opt and configured folders -------------------
        scan_roots = [(self.opt_dir, "opt")] + [(d, "custom") for d in extra_roots]
        for base, origin in scan_roots:
            try:
                with os.scandir(base) as it:
                    children = sorted(it, key=lambda e: e.name)
            except OSError:
                continue
            for child in children:
                path = os.path.normpath(child.path)
                if child.name.startswith(".") or ctx.is_claimed(path):
                    continue
                try:
                    is_dir = child.is_dir(follow_symlinks=False)
                    is_link = child.is_symlink()
                except OSError:
                    continue
                if is_link:
                    continue
                if owner_below(path):
                    continue  # package-managed (e.g. /opt/google -> google-chrome-stable)
                if not is_dir and not file_kind(path):
                    continue
                cand = candidate(path)
                cand.from_scan = cand.from_scan or origin

        # C. Executables and symlinks in bin folders --------------------------------
        bin_dirs = list(self.bin_dirs) + [os.path.join(home, ".local", "bin")]
        for bindir in bin_dirs:
            try:
                with os.scandir(bindir) as it:
                    items = sorted(it, key=lambda e: e.name)
            except OSError:
                continue
            for item in items:
                path = item.path
                if ctx.is_claimed(path) or owner(path):
                    continue
                if item.is_symlink():
                    real = os.path.realpath(path)
                    if not os.path.exists(real):
                        records.append(self._broken_link(path, real))
                        ctx.claim(path)
                        continue
                    attached = False
                    for root, cand in candidates.items():
                        if real == root or real.startswith(root + "/"):
                            cand.symlinks.append(path)
                            attached = True
                            break
                    if attached:
                        ctx.claim(path)
                    continue
                if bindir.startswith(home):
                    continue  # ~/.local/bin is full of tool scripts; only symlinks are evidence
                if not file_kind(path):
                    continue
                cand = candidate(path)
                cand.from_scan = cand.from_scan or "bin"

        # Turn candidates into records ---------------------------------------------
        for root, cand in candidates.items():
            if ctx.is_claimed(root):
                continue
            records.append(self._record_for(cand, home))
            ctx.claim(root)
        return records

    # -- record builders -----------------------------------------------------------
    def _record_for(self, cand: _Candidate, home: str) -> AppRecord:
        root = cand.root
        visible = [e for e in cand.launchers if e.visible]
        primary = visible[0] if visible else (cand.launchers[0] if cand.launchers else None)
        is_dir = os.path.isdir(root) and not os.path.islink(root)
        exe = cand.executable or (_main_executable(root, os.path.basename(root)) if is_dir else root)
        uninstaller = _find_uninstaller(root) if is_dir else ""
        in_home = root.startswith(home + os.sep)
        system_install = root.startswith(("/opt/", "/usr/local/"))

        if primary:
            confidence = Confidence.DETECTED
        elif system_install and is_dir and exe and file_kind(exe) == "elf":
            confidence = Confidence.DETECTED
        else:
            confidence = Confidence.UNKNOWN
        source = Source.VENDOR if system_install and (uninstaller or cand.from_system_launcher) else Source.MANUAL
        if confidence == Confidence.UNKNOWN and not primary:
            source = Source.UNKNOWN if not is_dir and not exe else source

        try:
            st = os.lstat(root)
            date = st.st_mtime
            size = st.st_size if not is_dir else None
        except OSError:
            date, size = None, None

        related = [e.path for e in cand.launchers if e is not primary] + cand.symlinks
        icon_path = _icon_file(primary.icon, home) if primary else ""
        if icon_path and (icon_path.startswith(home) or icon_path.startswith("/usr/local/")):
            related.append(icon_path)
        notes = []
        if uninstaller:
            notes.append(f"The vendor ships an uninstaller: {uninstaller}")
        if not primary:
            notes.append("No desktop launcher found; this may be a command-line tool or a component.")
        if not in_home and not system_install:
            notes.append("Installed outside the usual locations.")

        if primary and primary.name:
            name = primary.name
        else:
            name = _pretty_name(root) if is_dir else os.path.basename(root)
        return AppRecord(
            key=f"manual:{root}",
            name=name,
            source=source,
            package_manager=PackageManager.NONE,
            confidence=confidence,
            summary=(primary.comment or primary.generic_name) if primary else (
                "Manually installed folder" if is_dir else "Executable file"),
            icon=(primary.icon if primary and primary.icon else
                  ("application-x-executable" if exe else "folder")),
            package_name=os.path.basename(root),
            version=_read_version(root, os.path.basename(root)),
            install_date=date,
            install_path=root,
            exec_path=exe,
            exec_command=primary.exec_line if primary else "",
            desktop_file=primary.path if primary else "",
            desktop_id=primary.desktop_id if primary else "",
            categories=primary.categories if primary else [],
            size=size,
            hidden=bool(primary and (not primary.visible or not shown_in_current_desktop(
                primary.only_show_in, primary.not_show_in))),
            has_launcher=primary is not None,
            related_paths=related,
            notes=notes,
            extra={
                "vendor_uninstaller": uninstaller,
                # The folder was guessed from a script/interpreter target, not a native program.
                "root_from_script": bool(cand.executable) and file_kind(cand.executable) != "elf"
                and root != cand.executable,
                "launchers": [e.path for e in cand.launchers],
                "symlinks": cand.symlinks,
                "found_by": cand.from_scan or "launcher",
                "is_directory": is_dir,
                "terminal": bool(primary and primary.terminal),
            },
        )

    def _web_app(self, entry: DesktopEntry, home: str) -> AppRecord:
        browser = os.path.basename(entry.exec_target or entry.program)
        match = _BROWSER_HINT.search(entry.exec_target or "")
        browser_name = match.group(1).capitalize() if match else browser
        icon_path = _icon_file(entry.icon, home)
        related = [icon_path] if icon_path and icon_path.startswith(home) else []
        return AppRecord(
            key=f"webapp:{entry.path}",
            name=entry.name or entry.stem,
            source=Source.MANUAL,
            package_manager=PackageManager.NONE,
            confidence=Confidence.DETECTED,
            summary=entry.comment or f"Web app installed from {browser_name}",
            icon=entry.icon or "web-browser",
            package_name=entry.web_app_id,
            install_date=_mtime(entry.path),
            install_path=entry.path,
            exec_path=entry.exec_target,
            exec_command=entry.exec_line,
            desktop_file=entry.path,
            desktop_id=entry.desktop_id,
            categories=entry.categories or ["Network"],
            hidden=not entry.visible,
            related_paths=related,
            notes=[f"Web app (PWA) created by {browser_name}. Removing it here deletes the launcher; "
                   f"its site data stays in the browser profile."],
            extra={"web_app": True, "browser": browser_name, "launchers": [entry.path]},
        )

    def _custom_launcher(self, entry: DesktopEntry, pkg: str, home: str) -> AppRecord:
        return AppRecord(
            key=f"launcher:{entry.path}",
            name=entry.name or entry.stem,
            source=Source.MANUAL,
            package_manager=PackageManager.NONE,
            confidence=Confidence.DETECTED,
            summary=entry.comment or f"Custom launcher for {os.path.basename(entry.exec_target)}",
            icon=entry.icon or "application-x-executable",
            package_name=entry.desktop_id,
            install_date=_mtime(entry.path),
            install_path=entry.path,
            exec_path=entry.exec_target,
            exec_command=entry.exec_line,
            desktop_file=entry.path,
            desktop_id=entry.desktop_id,
            categories=entry.categories,
            hidden=not entry.visible,
            is_component=not entry.visible,
            notes=[f"Custom launcher for {entry.exec_target}, which belongs to the package '{pkg}'. "
                   "Removing it only deletes the launcher."],
            extra={"custom_launcher": True, "target_package": pkg, "launchers": [entry.path]},
        )

    def _broken_launcher(self, entry: DesktopEntry, home: str) -> AppRecord:
        what = entry.exec_target or entry.program or entry.exec_line or "nothing"
        return AppRecord(
            key=f"launcher:{entry.path}",
            name=entry.name or entry.stem,
            source=Source.UNKNOWN,
            package_manager=PackageManager.NONE,
            confidence=Confidence.UNKNOWN,
            summary="Launcher whose program could not be found",
            icon=entry.icon or "dialog-question",
            package_name=entry.desktop_id,
            install_date=_mtime(entry.path),
            install_path=entry.path,
            exec_command=entry.exec_line,
            desktop_file=entry.path,
            desktop_id=entry.desktop_id,
            categories=entry.categories,
            hidden=not entry.visible,
            notes=[f"Broken or unresolvable launcher: it starts '{what}', which does not exist "
                   "or cannot be resolved without running a shell."],
            extra={"broken_launcher": True, "launchers": [entry.path]},
        )


    def _broken_link(self, path: str, target: str) -> AppRecord:
        return AppRecord(
            key=f"link:{path}",
            name=os.path.basename(path),
            source=Source.UNKNOWN,
            package_manager=PackageManager.NONE,
            confidence=Confidence.UNKNOWN,
            summary="Symbolic link whose target no longer exists",
            icon="emblem-symbolic-link",
            package_name=os.path.basename(path),
            install_date=_mtime(path),
            install_path=path,
            exec_path=path,
            has_launcher=False,
            notes=[f"Broken symbolic link: it points to {target}, which does not exist. "
                   "It is probably left over from an uninstalled application."],
            extra={"broken_link": True, "link_target": target},
        )


def _mtime(path: str) -> float | None:
    try:
        return os.lstat(path).st_mtime
    except OSError:
        return None
