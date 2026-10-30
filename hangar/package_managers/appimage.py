"""AppImage discovery.

AppImages are identified by their magic bytes (``AI\\x01``/``AI\\x02`` at
offset 8 of the ELF header) or file extension. They are never executed:
metadata comes from the file name, from desktop launchers that point at the
file and from the embedded ``.upd_info`` ELF section.
"""

from __future__ import annotations

import os
import re
import stat
import struct

from ..core.application_model import AppRecord, Confidence, PackageManager, Source
from ..core.desktop_entry import DesktopEntry
from ..core.safety import is_icon_file
from .base import Provider, ScanContext

DEFAULT_DIRS = ("~/Applications", "~/AppImages", "~/Apps", "~/Downloads", "~/.local/bin", "~/bin",
                "~/Desktop", "/opt")

_ARCH_TOKENS = {"x86", "x86_64", "x64", "amd64", "i386", "i686", "aarch64", "arm64", "armhf", "linux",
                "appimage", "linux64", "64bit", "universal"}
_VERSION_RE = re.compile(r"^v?\d+(?:[._]\d+)*(?:[-+~]?(?:alpha|beta|rc|dev|build)?\d*)?$", re.I)


def appimage_type(path: str) -> int | None:
    """Return the AppImage type (1 or 2) by reading the magic bytes, else None."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        head = os.read(fd, 16)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(head) < 11 or head[:4] != b"\x7fELF" or head[8:10] != b"AI":
        return None
    return head[10] if head[10] in (1, 2) else None


def read_update_info(path: str) -> str:
    """Read the ``.upd_info`` ELF section without executing the file."""
    try:
        with open(path, "rb") as fh:
            ident = fh.read(64)
            if len(ident) < 64 or ident[:4] != b"\x7fELF":
                return ""
            is64 = ident[4] == 2
            endian = "<" if ident[5] == 1 else ">"
            if is64:
                shoff = struct.unpack_from(endian + "Q", ident, 0x28)[0]
                shentsize, shnum, shstrndx = struct.unpack_from(endian + "HHH", ident, 0x3A)
            else:
                shoff = struct.unpack_from(endian + "I", ident, 0x20)[0]
                shentsize, shnum, shstrndx = struct.unpack_from(endian + "HHH", ident, 0x2E)
            if not shoff or shnum == 0 or shnum > 512 or shstrndx >= shnum or shentsize < 40:
                return ""

            def section(index):
                fh.seek(shoff + index * shentsize)
                raw = fh.read(shentsize)
                if is64:
                    name, _type, _flags, _addr, offset, size = struct.unpack_from(endian + "IIQQQQ", raw, 0)
                else:
                    name, _type, _flags, _addr, offset, size = struct.unpack_from(endian + "IIIIII", raw, 0)
                return name, offset, size

            _, str_off, str_size = section(shstrndx)
            if str_size > 65536:
                return ""
            fh.seek(str_off)
            strtab = fh.read(str_size)
            for i in range(shnum):
                name_off, offset, size = section(i)
                end = strtab.find(b"\x00", name_off)
                if strtab[name_off:end] == b".upd_info":
                    if size > 4096:
                        return ""
                    fh.seek(offset)
                    return fh.read(size).split(b"\x00", 1)[0].decode("utf-8", "replace").strip()
    except (OSError, struct.error, ValueError):
        return ""
    return ""


def name_and_version(filename: str) -> tuple[str, str]:
    """Derive a display name and version from an AppImage file name."""
    stem = re.sub(r"\.appimage$", "", filename, flags=re.I)
    tokens = [t for t in re.split(r"[-_ ]+", stem) if t]
    name_tokens, version = [], ""
    for token in tokens:
        low = token.lower()
        if low in _ARCH_TOKENS:
            continue
        if _VERSION_RE.match(token) and name_tokens:
            if not version:
                version = token.lstrip("vV")
            continue
        if not version:
            name_tokens.append(token)
    words = name_tokens or [stem]
    name = " ".join(w if any(c.isupper() for c in w) else w.capitalize() for w in words)
    return name, version


def _iter_candidates(directory: str, depth: int = 1):
    try:
        with os.scandir(directory) as it:
            items = list(it)
    except OSError:
        return
    for item in items:
        try:
            if item.is_symlink():
                yield item.path, True
            elif item.is_file(follow_symlinks=False):
                yield item.path, False
            elif item.is_dir(follow_symlinks=False) and depth > 0 and not item.name.startswith("."):
                yield from _iter_candidates(item.path, depth - 1)
        except OSError:
            continue


def _looks_like_candidate(path: str) -> bool:
    if path.lower().endswith(".appimage"):
        return True
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and bool(st.st_mode & 0o111) and st.st_size > 512 * 1024


def _user_icon_path(icon: str) -> str:
    """Locate a user-installed icon file belonging to a launcher."""
    if not icon:
        return ""
    if os.path.isabs(icon):
        return icon if is_icon_file(icon) else ""
    base = os.path.join(os.path.expanduser("~"), ".local", "share", "icons", "hicolor")
    for size in ("scalable", "512x512", "256x256", "128x128", "64x64", "48x48"):
        for ext in (".svg", ".png"):
            candidate = os.path.join(base, size, "apps", icon + ext)
            if is_icon_file(candidate):
                return candidate
    return ""


class AppImageProvider(Provider):
    id = "appimage"
    label = "AppImage"
    icon_name = "application-x-executable-symbolic"
    description = "Portable AppImage files in your application folders"

    def available(self) -> bool:
        return True

    def tool_version(self) -> str:
        return "Built-in scanner"

    def scan(self, ctx: ScanContext) -> list[AppRecord]:
        dirs = ctx.appimage_dirs or [os.path.expanduser(d) for d in DEFAULT_DIRS]
        found: dict[str, int] = {}
        symlinks: list[tuple[str, str]] = []
        for directory in dirs:
            depth = 2 if directory == "/opt" else 1
            for path, is_link in _iter_candidates(directory, depth):
                if is_link:
                    target = os.path.realpath(path)
                    if target.lower().endswith(".appimage") or appimage_type(target):
                        symlinks.append((path, target))
                    continue
                if path in found or not _looks_like_candidate(path):
                    continue
                kind = appimage_type(path)
                if kind:
                    found[path] = kind
        # Symlinks in common bin directories pointing at AppImages
        for bindir in ("/usr/local/bin", os.path.join(ctx.home, ".local", "bin")):
            for path, is_link in _iter_candidates(bindir, 0):
                if is_link:
                    target = os.path.realpath(path)
                    if target in found:
                        symlinks.append((path, target))

        records = []
        for path, kind in sorted(found.items()):
            records.append(self._record(path, kind, ctx, [l for l, t in symlinks if t == path]))
        return records

    def _record(self, path: str, kind: int, ctx: ScanContext, links: list[str]) -> AppRecord:
        st = os.stat(path, follow_symlinks=False)
        launchers: list[DesktopEntry] = ctx.desktop_index.for_target(path)
        visible = [e for e in launchers if e.visible]
        primary = visible[0] if visible else (launchers[0] if launchers else None)
        file_name, file_version = name_and_version(os.path.basename(path))
        executable = bool(st.st_mode & 0o100)
        update_info = read_update_info(path) if kind == 2 else ""
        notes = []
        if not executable:
            notes.append("The file is not marked as executable.")
        if not launchers:
            notes.append("Not integrated: no desktop launcher points at this AppImage.")
        if update_info:
            notes.append("Embedded update information found; update checks for AppImages are not supported "
                         "by this app. Use the vendor's updater or AppImageUpdate.")
        related = [e.path for e in launchers] + links
        icon_file = _user_icon_path(primary.icon) if primary else ""
        if icon_file:
            related.append(icon_file)
        ctx.claim(path, *[e.path for e in launchers], *links)
        return AppRecord(
            key=f"appimage:{path}",
            name=(primary.name if primary and primary.name else file_name),
            source=Source.APPIMAGE,
            package_manager=PackageManager.NONE,
            confidence=Confidence.DETECTED,
            summary=(primary.comment if primary else "") or "Portable AppImage application",
            icon=(primary.icon if primary and primary.icon else "application-x-executable"),
            package_name=os.path.basename(path),
            version=file_version,
            install_date=st.st_mtime,
            install_path=path,
            exec_path=path,
            exec_command=primary.exec_line if primary else "",
            desktop_file=primary.path if primary else "",
            desktop_id=primary.desktop_id if primary else "",
            categories=primary.categories if primary else [],
            size=st.st_size,
            hidden=bool(primary and not primary.visible),
            has_launcher=True,
            related_paths=related,
            notes=notes,
            extra={
                "appimage_type": kind,
                "executable": executable,
                "update_info": update_info,
                "launchers": [e.path for e in launchers],
                "symlinks": links,
                "terminal": bool(primary and primary.terminal),
            },
        )
