"""APT / dpkg integration.

Inventory comes straight from the dpkg database (/var/lib/dpkg) which is
world-readable, so no privileges or subprocesses are needed for scanning.
Repository origin and update information use python3-apt when available.
"""

from __future__ import annotations

import bisect
import glob
import gzip
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime

from ..core.application_model import (
    AppRecord,
    Confidence,
    PackageManager,
    Source,
    UpdateInfo,
)
from .base import Provider, ScanContext, shown_in_current_desktop

log = logging.getLogger(__name__)

# Only ownership below these prefixes is indexed in memory. Other lookups fall
# back to `dpkg-query -S` (rare: launchers pointing into e.g. /usr/lib).
INDEX_PREFIXES = (
    "/opt/", "/usr/local/", "/usr/bin/", "/bin/", "/usr/sbin/", "/sbin/", "/usr/games/",
    "/usr/libexec/", "/usr/share/applications/", "/usr/share/pixmaps/", "/usr/share/icons/hicolor/",
)
_USRMERGE = ("/bin/", "/sbin/", "/lib/", "/lib64/", "/lib32/")
# dpkg never installs files here; skip the (slow) dpkg-query fallback.
_NEVER_PACKAGED = ("/home/", "/root/", "/tmp/", "/var/tmp/", "/media/", "/mnt/", "/snap/", "/var/lib/snapd/",
                   "/var/lib/flatpak/")


@dataclass
class PackageInfo:
    name: str
    version: str = ""
    architecture: str = ""
    installed_size: int | None = None  # bytes
    maintainer: str = ""
    section: str = ""
    priority: str = ""
    homepage: str = ""
    summary: str = ""
    description: str = ""
    essential: bool = False
    source: str = ""
    extra: dict = field(default_factory=dict)


def _parse_status(path: str) -> list[dict[str, str]]:
    paragraphs: list[dict[str, str]] = []
    current: dict[str, str] = {}
    last_key = None
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return paragraphs
    with fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line:
                if current:
                    paragraphs.append(current)
                current, last_key = {}, None
                continue
            if line[0] in " \t":
                if last_key:
                    current[last_key] += "\n" + line[1:]
                continue
            key, _, value = line.partition(":")
            last_key = key
            current[key] = value.strip()
    if current:
        paragraphs.append(current)
    return paragraphs


def _clean_description(text: str) -> tuple[str, str]:
    lines = text.split("\n")
    summary = lines[0].strip()
    body = []
    for raw in lines[1:]:
        raw = raw.strip()
        body.append("" if raw == "." else raw)
    return summary, "\n".join(body).strip()


class DpkgDatabase:
    """Read-only view of the dpkg database."""

    def __init__(self, admindir: str = "/var/lib/dpkg", log_dir: str = "/var/log"):
        self.admindir = admindir
        self.log_dir = log_dir
        self.packages: dict[str, PackageInfo] = {}
        self.native_arch = ""
        self._owners: dict[str, str] = {}
        self._sorted: list[str] = []
        self._install_dates: dict[str, float] = {}
        self._query_cache: dict[str, str | None] = {}
        self.loaded = False

    # -- loading -----------------------------------------------------------
    def load(self) -> "DpkgDatabase":
        self._load_status()
        self._load_lists()
        self._load_logs()
        self.loaded = True
        return self

    def _pkg_key(self, name: str, arch: str) -> str:
        if not arch or arch in ("all", self.native_arch):
            return name
        return f"{name}:{arch}"

    def _normalize_owner(self, list_name: str) -> str:
        if ":" in list_name:
            name, arch = list_name.split(":", 1)
            return self._pkg_key(name, arch)
        return list_name

    def _load_status(self) -> None:
        paragraphs = _parse_status(os.path.join(self.admindir, "status"))
        arch_count: dict[str, int] = {}
        installed = []
        for p in paragraphs:
            status = p.get("Status", "")
            if not status.endswith(" installed"):
                continue
            installed.append(p)
            arch = p.get("Architecture", "")
            if arch and arch != "all":
                arch_count[arch] = arch_count.get(arch, 0) + 1
        self.native_arch = max(arch_count, key=arch_count.get) if arch_count else ""
        for p in installed:
            key = self._pkg_key(p.get("Package", ""), p.get("Architecture", ""))
            summary, description = _clean_description(p.get("Description", ""))
            size = p.get("Installed-Size", "")
            self.packages[key] = PackageInfo(
                name=key,
                version=p.get("Version", ""),
                architecture=p.get("Architecture", ""),
                installed_size=int(size) * 1024 if size.isdigit() else None,
                maintainer=p.get("Maintainer", ""),
                section=p.get("Section", ""),
                priority=p.get("Priority", ""),
                homepage=p.get("Homepage", ""),
                summary=summary,
                description=description,
                essential=p.get("Essential", "").lower() == "yes",
                source=p.get("Source", "").split(" ")[0],
                extra={k: p[k] for k in ("Vcs-Browser", "Vcs-Git") if p.get(k)},
            )

    def _load_lists(self) -> None:
        owners: dict[str, str] = {}
        info_dir = os.path.join(self.admindir, "info")
        for list_file in glob.glob(os.path.join(info_dir, "*.list")):
            owner = self._normalize_owner(os.path.basename(list_file)[:-5])
            try:
                with open(list_file, encoding="utf-8", errors="surrogateescape") as fh:
                    for line in fh:
                        path = line.rstrip("\n")
                        if path.startswith(INDEX_PREFIXES) or path in ("/opt", "/usr/local"):
                            owners.setdefault(path, owner)
            except OSError:
                continue
        self._owners = owners
        self._sorted = sorted(owners)

    def _load_logs(self) -> None:
        dates: dict[str, float] = {}
        pattern = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) install (\S+) <none> ")
        files = sorted(glob.glob(os.path.join(self.log_dir, "dpkg.log*")), reverse=True)
        for path in files:
            try:
                opener = gzip.open if path.endswith(".gz") else open
                with opener(path, "rt", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if " install " not in line:
                            continue
                        m = pattern.match(line)
                        if not m:
                            continue
                        name = m.group(2)
                        if ":" in name:
                            n, a = name.split(":", 1)
                            name = self._pkg_key(n, a)
                        ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
                        dates.setdefault(name, ts)
            except (OSError, EOFError, ValueError):
                continue
        self._install_dates = dates

    # -- queries -------------------------------------------------------------
    def package(self, name: str) -> PackageInfo | None:
        return self.packages.get(name)

    def install_date(self, name: str) -> float | None:
        if name in self._install_dates:
            return self._install_dates[name]
        info_dir = os.path.join(self.admindir, "info")
        candidates = [os.path.join(info_dir, f"{name}.list")]
        if ":" not in name and self.native_arch:
            candidates.append(os.path.join(info_dir, f"{name}:{self.native_arch}.list"))
        for c in candidates:
            try:
                return os.stat(c).st_mtime
            except OSError:
                continue
        return None

    def _variants(self, path: str) -> list[str]:
        variants = [path]
        if path.startswith(_USRMERGE):
            variants.append("/usr" + path)
        elif path.startswith("/usr/") and ("/" + path[5:]).startswith(_USRMERGE):
            variants.append(path[4:])
        real = os.path.realpath(path)
        if real not in variants:
            variants.append(real)
        return variants

    def owner(self, path: str) -> str | None:
        """Package owning ``path`` (exact match), or None."""
        if not path or path.startswith(_NEVER_PACKAGED):
            return None
        for variant in self._variants(path):
            owner = self._owners.get(variant)
            if owner:
                return owner
        for variant in self._variants(path):
            if variant.startswith(INDEX_PREFIXES):
                continue
            return self._query(variant)
        return None

    def owner_below(self, directory: str) -> str | None:
        """Package owning ``directory`` itself or anything below it."""
        directory = os.path.normpath(directory)
        if (directory + "/").startswith(_NEVER_PACKAGED):
            return None
        exact = self._owners.get(directory)
        if exact:
            return exact
        prefix = directory + "/"
        i = bisect.bisect_left(self._sorted, prefix)
        if i < len(self._sorted) and self._sorted[i].startswith(prefix):
            return self._owners[self._sorted[i]]
        if not directory.startswith(tuple(p.rstrip("/") for p in INDEX_PREFIXES)):
            return self._query(directory)
        return None

    def _query(self, path: str) -> str | None:
        if path in self._query_cache:
            return self._query_cache[path]
        result = None
        if shutil.which("dpkg-query"):
            try:
                proc = subprocess.run(
                    ["dpkg-query", "-S", "--", path],
                    capture_output=True, text=True, timeout=10,
                    env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
                )
                for line in proc.stdout.splitlines():
                    if line.startswith("diversion by"):
                        continue
                    pkgs, _, owned = line.partition(": ")
                    if owned.strip() == path:
                        result = self._normalize_owner(pkgs.split(",")[0].strip())
                        break
            except (OSError, subprocess.SubprocessError):
                result = None
        self._query_cache[path] = result
        return result


# ---------------------------------------------------------------------------
# Repository origins (python3-apt, optional)
# ---------------------------------------------------------------------------

def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def distro_tokens() -> set[str]:
    tokens = {"ubuntu", "debian", "canonical"}
    try:
        with open("/etc/os-release", encoding="utf-8") as fh:
            for line in fh:
                key, _, value = line.strip().partition("=")
                value = value.strip('"')
                if key in ("ID", "ID_LIKE"):
                    tokens.update(_norm(v) for v in value.split() if v)
    except OSError:
        pass
    return {t for t in tokens if len(t) >= 4}


@dataclass
class OriginInfo:
    label: str
    is_distro: bool
    local_only: bool = False


class AptCacheView:
    """Thin optional wrapper around python3-apt."""

    def __init__(self):
        self.cache = None
        try:
            import apt  # type: ignore

            self.cache = apt.Cache()
        except Exception as exc:  # python3-apt missing or cache unreadable
            log.info("python3-apt unavailable: %s", exc)
        self.tokens = distro_tokens()

    @property
    def available(self) -> bool:
        return self.cache is not None

    def _lookup(self, name: str):
        if self.cache is None:
            return None
        try:
            return self.cache[name]
        except KeyError:
            return None

    def _describe(self, origins) -> OriginInfo | None:
        for o in origins:
            if not o.origin and not o.site:
                continue
            text = " ".join(x for x in (o.origin, o.label, o.site) if x)
            is_distro = any(t in _norm(text) for t in self.tokens) or o.site.endswith(
                ("ubuntu.com", "debian.org", "canonical.com"))
            label = o.label or o.origin or o.site
            if ("/" in label or label.startswith(".")) and o.site:
                label = o.site  # e.g. "namespaces/moz-fx-…/repositories/mozilla" -> packages.mozilla.org
            if o.origin.startswith("LP-PPA-"):
                label = "PPA: " + (o.label or o.origin[7:])
            return OriginInfo(label=label, is_distro=is_distro)
        return None

    def origin(self, name: str) -> OriginInfo | None:
        pkg = self._lookup(name)
        if pkg is None or pkg.installed is None:
            return None
        found = self._describe(pkg.installed.origins)
        if found:
            return found
        cand = pkg.candidate
        if cand is not None and cand.version != pkg.installed.version:
            found = self._describe(cand.origins)
            if found:
                return found
        return OriginInfo(label="Local package (no repository)", is_distro=False, local_only=True)

    def is_auto_installed(self, name: str) -> bool | None:
        pkg = self._lookup(name)
        return None if pkg is None else bool(pkg.is_auto_installed)


def _heuristic_origin(info: PackageInfo, tokens: set[str]) -> OriginInfo:
    text = _norm(info.maintainer + " " + info.version)
    if any(t in text for t in tokens):
        return OriginInfo(label="Distribution", is_distro=True)
    return OriginInfo(label=info.maintainer.split("<")[0].strip() or "Third party", is_distro=False)


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

USER_ONLY_DIR_MARKERS = ("/.local/share/applications", "/snapd/desktop/", "/flatpak/exports/")


class AptProvider(Provider):
    id = "apt"
    label = "APT"
    icon_name = "package-x-generic-symbolic"
    description = "Debian packages from Ubuntu and third-party repositories (dpkg database)"

    def available(self) -> bool:
        return os.path.exists("/var/lib/dpkg/status") and shutil.which("apt-get") is not None

    def tool_version(self) -> str:
        try:
            out = subprocess.run(["apt-get", "--version"], capture_output=True, text=True, timeout=5)
            return out.stdout.splitlines()[0] if out.stdout else ""
        except (OSError, subprocess.SubprocessError):
            return ""

    def supports_updates(self) -> bool:
        return True

    def scan(self, ctx: ScanContext) -> list[AppRecord]:
        dpkg = ctx.dpkg
        if dpkg is None:
            return []
        cache = AptCacheView()
        tokens = cache.tokens
        records: list[AppRecord] = []
        seen_ids: set[str] = set()
        for entry in ctx.desktop_index.entries:
            if any(marker in entry.path for marker in USER_ONLY_DIR_MARKERS):
                continue
            if entry.desktop_id in seen_ids:
                continue  # shadowed by an earlier data dir
            owner = dpkg.owner(entry.path)
            if not owner:
                continue
            info = dpkg.package(owner) or PackageInfo(name=owner)
            seen_ids.add(entry.desktop_id)
            origin = cache.origin(owner) if cache.available else None
            if origin is None:
                origin = _heuristic_origin(info, tokens)
            source = Source.APT if origin.is_distro else Source.VENDOR

            exec_path = entry.exec_target
            install_path = ""
            if exec_path and os.path.exists(exec_path):
                install_path = os.path.dirname(os.path.realpath(exec_path))
            hidden = not entry.visible or not shown_in_current_desktop(entry.only_show_in, entry.not_show_in)
            is_component = info.essential or info.priority in ("required", "important")
            notes = []
            if origin.local_only:
                notes.append("Installed from a downloaded .deb file; no repository provides updates.")
            auto = cache.is_auto_installed(owner) if cache.available else None
            record = AppRecord(
                key=f"apt:{owner}:{entry.desktop_id}",
                name=entry.name or owner,
                source=source,
                package_manager=PackageManager.APT,
                confidence=Confidence.MANAGED,
                summary=entry.comment or entry.generic_name or info.summary,
                description=info.description or info.summary,
                icon=entry.icon,
                package_name=owner,
                version=info.version,
                install_date=dpkg.install_date(owner),
                install_path=install_path,
                exec_path=exec_path,
                exec_command=entry.exec_line,
                desktop_file=entry.path,
                desktop_id=entry.desktop_id,
                categories=entry.categories,
                size=info.installed_size,
                origin=origin.label,
                homepage=info.homepage,
                is_component=is_component,
                hidden=hidden,
                has_launcher=True,
                notes=notes,
                extra={
                    "section": info.section,
                    "priority": info.priority,
                    "maintainer": info.maintainer,
                    "architecture": info.architecture,
                    "package_summary": info.summary,
                    "auto_installed": auto,
                    "terminal": entry.terminal,
                    # Downloaded .deb without a repository: candidates for GitHub release tracking.
                    "local_deb": origin.local_only,
                    "vcs": info.extra.get("Vcs-Browser") or info.extra.get("Vcs-Git", ""),
                },
            )
            records.append(record)
            ctx.managed_desktop_ids.add(entry.desktop_id)
            ctx.claim(entry.path)
        return records

    # -- updates -------------------------------------------------------------
    def check_updates(self, records: list[AppRecord]) -> list[UpdateInfo]:
        apps_by_pkg: dict[str, list[AppRecord]] = {}
        for r in records:
            if r.package_manager == PackageManager.APT:
                apps_by_pkg.setdefault(r.package_name, []).append(r)
        try:
            import apt  # type: ignore

            return self._updates_python_apt(apt.Cache(), apps_by_pkg)
        except ImportError:
            return self._updates_cli(apps_by_pkg)

    def _updates_python_apt(self, cache, apps_by_pkg) -> list[UpdateInfo]:
        updates = []
        for pkg in cache:
            if not pkg.is_upgradable:
                continue
            cand = pkg.candidate
            apps = apps_by_pkg.get(pkg.name, [])
            origin = ""
            for o in cand.origins:
                if o.origin or o.label:
                    origin = o.label or o.origin
                    if ("/" in origin or origin.startswith(".")) and o.site:
                        origin = o.site
                    break
            phased = bool(cand.record.get("Phased-Update-Percentage")) if cand.record else False
            updates.append(UpdateInfo(
                source="apt",
                package_name=pkg.name,
                name=apps[0].name if apps else pkg.name,
                current_version=pkg.installed.version if pkg.installed else "",
                new_version=cand.version,
                download_size=cand.size,
                is_application=bool(apps),
                origin=origin,
                phased=phased,
                app_keys=[a.key for a in apps],
            ))
        return updates

    def _updates_cli(self, apps_by_pkg) -> list[UpdateInfo]:
        try:
            proc = subprocess.run(["apt", "list", "--upgradable"], capture_output=True, text=True,
                                  timeout=60, env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"})
        except (OSError, subprocess.SubprocessError):
            return []
        updates = []
        rx = re.compile(r"^([^/\s]+)/(\S+)\s+(\S+)\s+(\S+)\s+\[upgradable from: ([^\]]+)\]")
        for line in proc.stdout.splitlines():
            m = rx.match(line)
            if not m:
                continue
            name = m.group(1)
            apps = apps_by_pkg.get(name, [])
            updates.append(UpdateInfo(
                source="apt", package_name=name, name=apps[0].name if apps else name,
                current_version=m.group(5), new_version=m.group(3),
                is_application=bool(apps), origin=m.group(2), app_keys=[a.key for a in apps],
            ))
        return updates


def apt_lists_age() -> float | None:
    """Timestamp of the last successful `apt update`, if known."""
    for candidate in ("/var/lib/apt/periodic/update-success-stamp", "/var/lib/apt/lists/partial",
                      "/var/cache/apt/pkgcache.bin"):
        try:
            return os.stat(candidate).st_mtime
        except OSError:
            continue
    return None
