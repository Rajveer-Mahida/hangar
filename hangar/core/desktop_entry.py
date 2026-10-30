"""Freedesktop .desktop entry discovery and parsing.

Parsing uses GLib.KeyFile so localisation and escaping follow the
specification. Nothing here executes anything: Exec lines are only parsed.
"""

from __future__ import annotations

import os
import shlex
import shutil
import stat
from dataclasses import dataclass, field

from gi.repository import GLib

GROUP = "Desktop Entry"

# Field codes defined by the Desktop Entry specification.
_FIELD_CODES = {"%f", "%F", "%u", "%U", "%d", "%D", "%n", "%N", "%i", "%c", "%k", "%v", "%m"}

# Launch wrappers whose real target is a later argument.
_WRAPPERS = {"env", "nice", "ionice", "taskset", "nohup", "gamemoderun", "prime-run", "pkexec", "sudo"}
_SHELLS = {"sh", "bash", "dash", "zsh"}
_INTERPRETERS = {"python", "python3", "java", "mono", "node", "perl", "ruby", "wine"}

DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin"


@dataclass
class DesktopEntry:
    path: str
    desktop_id: str
    name: str = ""
    generic_name: str = ""
    comment: str = ""
    icon: str = ""
    exec_line: str = ""
    try_exec: str = ""
    entry_type: str = "Application"
    categories: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    no_display: bool = False
    hidden: bool = False
    terminal: bool = False
    only_show_in: list[str] = field(default_factory=list)
    not_show_in: list[str] = field(default_factory=list)
    startup_wm_class: str = ""
    flatpak_id: str = ""
    snap_instance: str = ""
    # Resolved launch target
    program: str = ""            # program token as written (after wrappers)
    exec_target: str = ""        # absolute path if resolvable
    exec_args: list[str] = field(default_factory=list)

    @property
    def is_application(self) -> bool:
        return self.entry_type == "Application"

    @property
    def visible(self) -> bool:
        return not (self.no_display or self.hidden)

    @property
    def stem(self) -> str:
        return self.desktop_id[:-8] if self.desktop_id.endswith(".desktop") else self.desktop_id

    @property
    def web_app_id(self) -> str:
        """Chromium-based browser web apps (PWAs) carry --app-id=..."""
        for arg in self.exec_args:
            if arg.startswith("--app-id="):
                return arg.split("=", 1)[1]
        return ""


def _get_string(kf: GLib.KeyFile, key: str, localized: bool = False) -> str:
    try:
        if localized:
            return kf.get_locale_string(GROUP, key, None) or ""
        return kf.get_string(GROUP, key) or ""
    except GLib.Error:
        return ""


def _get_bool(kf: GLib.KeyFile, key: str) -> bool:
    try:
        return bool(kf.get_boolean(GROUP, key))
    except GLib.Error:
        return False


def _get_list(kf: GLib.KeyFile, key: str, localized: bool = False) -> list[str]:
    try:
        if localized:
            values = kf.get_locale_string_list(GROUP, key, None)
        else:
            values = kf.get_string_list(GROUP, key)
        return [v for v in values if v]
    except GLib.Error:
        return []


def split_exec(exec_line: str) -> list[str]:
    """Split an Exec value into argv, dropping field codes."""
    if not exec_line:
        return []
    try:
        tokens = shlex.split(exec_line, posix=True)
    except ValueError:
        tokens = exec_line.split()
    return [t for t in tokens if t not in _FIELD_CODES and t != "%%"]


def resolve_program(argv: list[str], search_path: str | None = None) -> tuple[str, str, list[str]]:
    """Return (program_token, absolute_target, remaining_args) for an Exec argv.

    Skips launch wrappers such as ``env VAR=1 prog`` and interpreters such as
    ``python3 /opt/app/main.py`` (the script is treated as the target).
    Nothing is executed.
    """
    path = search_path or os.environ.get("PATH") or DEFAULT_PATH
    if "/snap/bin" not in path.split(":"):
        path = path + ":/snap/bin"
    i = 0
    while i < len(argv):
        token = argv[i]
        base = os.path.basename(token)
        if base in _WRAPPERS:
            i += 1
            while i < len(argv) and (argv[i].startswith("-") or "=" in argv[i].split("/")[0]):
                i += 1
            continue
        if base in _SHELLS and i + 1 < len(argv) and argv[i + 1] == "-c":
            # Shell snippets cannot be resolved reliably.
            return token, "", argv[i + 1:]
        if base.rstrip("0123456789.") in _INTERPRETERS:
            j = i + 1
            while j < len(argv) and argv[j].startswith("-"):
                j += 1
            if j < len(argv) and os.path.isabs(argv[j]):
                return argv[j], argv[j], argv[j + 1:]
        target = token if os.path.isabs(token) else (shutil.which(token, path=path) or "")
        return token, target, argv[i + 1:]
    return "", "", []


def parse_desktop_file(path: str, desktop_id: str | None = None) -> DesktopEntry | None:
    """Parse one .desktop file. Returns None for unreadable or invalid files."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if stat.S_ISLNK(st.st_mode):
        # Follow symlinked launchers only if they resolve to a regular file.
        try:
            if not stat.S_ISREG(os.stat(path).st_mode):
                return None
        except OSError:
            return None
    elif not stat.S_ISREG(st.st_mode):
        return None
    if st.st_size > 1024 * 1024:
        return None

    kf = GLib.KeyFile()
    try:
        kf.load_from_file(path, GLib.KeyFileFlags.NONE)
    except GLib.Error:
        return None
    if not kf.has_group(GROUP):
        return None

    entry = DesktopEntry(path=path, desktop_id=desktop_id or os.path.basename(path))
    entry.entry_type = _get_string(kf, "Type") or "Application"
    entry.name = _get_string(kf, "Name", localized=True)
    entry.generic_name = _get_string(kf, "GenericName", localized=True)
    entry.comment = _get_string(kf, "Comment", localized=True)
    entry.icon = _get_string(kf, "Icon")
    entry.exec_line = _get_string(kf, "Exec")
    entry.try_exec = _get_string(kf, "TryExec")
    entry.categories = _get_list(kf, "Categories")
    entry.keywords = _get_list(kf, "Keywords", localized=True)
    entry.no_display = _get_bool(kf, "NoDisplay")
    entry.hidden = _get_bool(kf, "Hidden")
    entry.terminal = _get_bool(kf, "Terminal")
    entry.only_show_in = _get_list(kf, "OnlyShowIn")
    entry.not_show_in = _get_list(kf, "NotShowIn")
    entry.startup_wm_class = _get_string(kf, "StartupWMClass")
    entry.flatpak_id = _get_string(kf, "X-Flatpak")
    entry.snap_instance = _get_string(kf, "X-SnapInstanceName")

    argv = split_exec(entry.exec_line)
    entry.program, entry.exec_target, entry.exec_args = resolve_program(argv)
    if not entry.exec_target and entry.try_exec:
        _, entry.exec_target, _ = resolve_program([entry.try_exec])
    return entry


def scan_desktop_dir(directory: str, prefix: str = "") -> list[DesktopEntry]:
    """Parse all .desktop files in a directory (and subdirectories, per spec).

    Subdirectory entries get ids like ``kde-foo.desktop``. Symlinked
    directories are not followed.
    """
    entries: list[DesktopEntry] = []
    try:
        with os.scandir(directory) as it:
            items = sorted(it, key=lambda e: e.name)
    except OSError:
        return entries
    for item in items:
        try:
            if item.is_dir(follow_symlinks=False):
                if prefix.count("-") < 3:  # bounded recursion depth
                    entries.extend(scan_desktop_dir(item.path, f"{prefix}{item.name}-"))
                continue
        except OSError:
            continue
        if not item.name.endswith(".desktop"):
            continue
        parsed = parse_desktop_file(item.path, prefix + item.name)
        if parsed is not None:
            entries.append(parsed)
    return entries


def user_data_dir() -> str:
    return os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")


def application_dirs() -> list[str]:
    """Desktop entry directories in XDG precedence order (user first)."""
    dirs = [os.path.join(user_data_dir(), "applications")]
    data_dirs = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    for d in data_dirs.split(":"):
        if d:
            dirs.append(os.path.join(d, "applications"))
    for extra in (
        "/usr/local/share/applications",
        "/usr/share/applications",
        "/var/lib/snapd/desktop/applications",
        "/var/lib/flatpak/exports/share/applications",
        os.path.join(user_data_dir(), "flatpak", "exports", "share", "applications"),
    ):
        dirs.append(extra)
    seen: set[str] = set()
    result = []
    for d in dirs:
        norm = os.path.normpath(d)
        if norm not in seen:
            seen.add(norm)
            result.append(norm)
    return result


class DesktopIndex:
    """All desktop entries on the system, indexed for lookups."""

    def __init__(self, directories: list[str] | None = None):
        self.directories = directories if directories is not None else application_dirs()
        self.entries: list[DesktopEntry] = []
        self.by_path: dict[str, DesktopEntry] = {}
        self.by_target: dict[str, list[DesktopEntry]] = {}
        for directory in self.directories:
            for entry in scan_desktop_dir(directory):
                if not entry.is_application:
                    continue
                self.entries.append(entry)
                self.by_path[entry.path] = entry
                if entry.exec_target:
                    self.by_target.setdefault(entry.exec_target, []).append(entry)
                    real = os.path.realpath(entry.exec_target)
                    if real != entry.exec_target:
                        self.by_target.setdefault(real, []).append(entry)

    def in_directory(self, directory: str) -> list[DesktopEntry]:
        directory = os.path.normpath(directory) + os.sep
        return [e for e in self.entries if e.path.startswith(directory)]

    def for_target(self, path: str) -> list[DesktopEntry]:
        found = list(self.by_target.get(path, []))
        real = os.path.realpath(path)
        if real != path:
            found.extend(e for e in self.by_target.get(real, []) if e not in found)
        return found

    def referencing_prefix(self, prefix: str) -> list[DesktopEntry]:
        """Entries whose launch target lives below ``prefix``."""
        prefix = os.path.normpath(prefix) + os.sep
        out = []
        for entry in self.entries:
            target = entry.exec_target
            if not target:
                continue
            if target.startswith(prefix) or os.path.realpath(target).startswith(prefix):
                out.append(entry)
        return out
