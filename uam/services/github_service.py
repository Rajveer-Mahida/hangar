"""New-release detection for software installed from GitHub releases.

Covers AppImages, manual installs and ``.deb`` files that were downloaded by
hand (no APT repository, so APT never sees their updates).

How an app is linked to a repository, strongest first:
  1. a decision the user made in the app (stored in SQLite);
  2. the AppImage's embedded update information (``gh-releases-zsync|owner|repo|…``);
  3. the package's ``Homepage`` / ``Vcs-*`` fields when they name a repository.

Checks use the public GitHub REST API (60 requests/hour without a token, so
results are cached with ETags and only refreshed on the store interval).
Set ``GITHUB_TOKEN`` in the environment for a higher limit; it is never stored.
Nothing is downloaded or installed without an explicit user action.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from gi.repository import Gio, GLib

from .. import VERSION
from ..core.application_model import AppRecord, PackageManager, Source, UpdateInfo
from ..core.database import Database
from ..core.safety import home_dir, removal_problem
from ..package_managers.appimage import appimage_type, name_and_version
from .operation_service import Operation, PythonStep

API = "https://api.github.com"
OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
REPO_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
GITHUB_URL_RE = re.compile(r"github\.com[/:]([A-Za-z0-9-]{1,39})(?:/([A-Za-z0-9._-]{1,100}))?", re.I)
MAX_DOWNLOAD = 4 * 1024 ** 3
RELEASES_PER_CHECK = 15

_ARCH_OK = re.compile(r"(x86[_-]64|amd64|x64|linux64)", re.I)
_ARCH_BAD = re.compile(r"(arm64|aarch64|armhf|armv7|i386|i686|ppc64|s390x|riscv)", re.I)


class GitHubError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Links between installed apps and repositories
# ---------------------------------------------------------------------------

def normalize_repo(text: str) -> str | None:
    """'owner/repo', a github.com URL or an SSH remote -> 'owner/repo' (validated)."""
    text = (text or "").strip()
    m = GITHUB_URL_RE.search(text)
    if m:
        owner, repo = m.group(1), m.group(2)
    elif text.count("/") == 1:
        owner, repo = text.split("/")
    else:
        return None
    if not repo:
        return None
    repo = re.sub(r"\.git$", "", repo)
    if not OWNER_RE.match(owner) or not REPO_NAME_RE.match(repo) or repo in (".", ".."):
        return None
    return f"{owner}/{repo}"


def github_org(text: str) -> str | None:
    """Organisation named by an owner-only link such as https://github.com/gstride."""
    m = GITHUB_URL_RE.search(text or "")
    if m and not m.group(2) and OWNER_RE.match(m.group(1)):
        return m.group(1)
    return None


def is_trackable(record: AppRecord) -> bool:
    """Software whose updates no package manager delivers."""
    extra = record.extra
    if extra.get("web_app") or extra.get("custom_launcher") or extra.get("broken_launcher") or extra.get("broken_link"):
        return False
    if record.package_manager == PackageManager.NONE:
        return True
    return record.package_manager == PackageManager.APT and bool(extra.get("local_deb"))


def link_key(record: AppRecord) -> str:
    """Stable identity that survives updates (AppImage file names contain the version)."""
    if record.package_manager == PackageManager.APT:
        return f"apt:{record.package_name}"
    if record.source == Source.APPIMAGE:
        name, _ = name_and_version(os.path.basename(record.install_path))
        return f"appimage:{re.sub(r'[^a-z0-9]', '', name.lower())}"
    return record.key


def detected_repo(record: AppRecord) -> tuple[str, str] | None:
    """(repo, origin description) found in the app's own metadata."""
    update_info = record.extra.get("update_info", "")
    if update_info.startswith("gh-releases"):
        parts = update_info.split("|")
        if len(parts) >= 3:
            repo = normalize_repo(f"{parts[1]}/{parts[2]}")
            if repo:
                return repo, "AppImage update information"
    for value, origin in ((record.homepage, "package homepage"), (record.extra.get("vcs", ""), "package source")):
        repo = normalize_repo(value) if value and "github.com" in value.lower() else None
        if repo:
            return repo, origin
    return None


@dataclass
class Link:
    repo: str
    origin: str          # description shown to the user
    user: bool           # chosen by the user


def resolve_link(record: AppRecord, stored: dict[str, tuple[str, str]]) -> Link | None:
    decision = stored.get(link_key(record))
    if decision is not None:
        repo, origin = decision
        if origin == "none" or not repo:
            return None  # the user switched tracking off for this app
        return Link(repo, "set by you", True)
    found = detected_repo(record)
    if found:
        return Link(found[0], f"detected from {found[1]}", False)
    return None


def org_hint(record: AppRecord) -> str | None:
    return github_org(record.homepage) or github_org(record.extra.get("vcs", ""))


# ---------------------------------------------------------------------------
# Versions and release selection
# ---------------------------------------------------------------------------

_VERSION_RE = re.compile(r"\d+(?:[._]\d+)*")


def version_tuple(text: str) -> tuple[int, ...] | None:
    text = (text or "").strip()
    text = re.sub(r"^\d+:", "", text)  # Debian epoch
    m = _VERSION_RE.search(text)
    if not m:
        return None
    return tuple(int(p) for p in re.split(r"[._]", m.group(0)))


def compare_versions(installed: str, latest: str) -> int | None:
    """-1 if ``latest`` is newer, 0 if equal, 1 if installed is newer, None if unknown."""
    a, b = version_tuple(installed), version_tuple(latest)
    if a is None or b is None:
        return None
    width = max(len(a), len(b))
    a, b = a + (0,) * (width - len(a)), b + (0,) * (width - len(b))
    return (a > b) - (a < b)


def install_kind(record: AppRecord) -> str:
    if record.package_manager == PackageManager.APT:
        return "deb"
    if record.source == Source.APPIMAGE:
        return "appimage"
    return "any"


def matching_asset(release: dict, kind: str) -> dict | None:
    suffix = {"deb": ".deb", "appimage": ".appimage"}.get(kind)
    if suffix is None:
        return None
    candidates = []
    for asset in release.get("assets", []):
        name = asset.get("name", "")
        if not name.lower().endswith(suffix) or _ARCH_BAD.search(name):
            continue
        candidates.append((0 if _ARCH_OK.search(name) else 1, name, asset))
    candidates.sort(key=lambda c: (c[0], c[1]))
    return candidates[0][2] if candidates else None


def pick_release(releases: list[dict], kind: str) -> tuple[dict | None, dict | None]:
    """Newest stable release with an asset for this kind of install; otherwise the newest stable release.

    Monorepos publish several products (e.g. a macOS app and a Linux .deb), so
    the newest release is not necessarily the one for this app.
    """
    stable = [r for r in releases if not r.get("draft") and not r.get("prerelease")]
    for release in stable:
        asset = matching_asset(release, kind)
        if asset:
            return release, asset
    return (stable[0] if stable else None), None


def _trim(releases: list[dict]) -> list[dict]:
    keep = ("tag_name", "name", "html_url", "published_at", "prerelease", "draft")
    out = []
    for r in releases[:RELEASES_PER_CHECK]:
        item = {k: r.get(k) for k in keep}
        item["assets"] = [{k: a.get(k) for k in ("name", "size", "browser_download_url", "digest")}
                          for a in r.get("assets", [])]
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

class GitHubClient:
    def __init__(self, timeout: float = 20.0):
        self.timeout = timeout
        self.rate_remaining: int | None = None
        self.rate_reset: float | None = None

    def _request(self, path: str, etag: str | None = None):
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"hangar/{VERSION}",
        }
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if etag:
            headers["If-None-Match"] = etag
        req = urllib.request.Request(API + path, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                self._note_rate(resp.headers)
                return resp.status, resp.headers.get("ETag"), json.loads(resp.read(8 * 1024 * 1024))
        except urllib.error.HTTPError as exc:
            self._note_rate(exc.headers)
            if exc.code == 304:
                return 304, etag, None
            if exc.code == 429 or (exc.code == 403 and self.rate_remaining == 0):
                when = time.strftime("%H:%M", time.localtime(self.rate_reset)) if self.rate_reset else "a minute"
                raise GitHubError(f"GitHub's request limit was reached; try again after {when} "
                                  "(or set GITHUB_TOKEN for a higher limit).", exc.code) from exc
            if exc.code == 404:
                raise GitHubError("Repository not found (or it is private).", 404) from exc
            if exc.code == 422:
                raise GitHubError("That GitHub account does not exist or has no public repositories.", 422) from exc
            raise GitHubError(f"GitHub returned HTTP {exc.code}.", exc.code) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise GitHubError(f"Could not reach GitHub: {getattr(exc, 'reason', exc)}") from exc

    def _note_rate(self, headers) -> None:
        if headers is None:
            return
        remaining, reset = headers.get("X-RateLimit-Remaining"), headers.get("X-RateLimit-Reset")
        if remaining and remaining.isdigit():
            self.rate_remaining = int(remaining)
        if reset and reset.isdigit():
            self.rate_reset = float(reset)

    def releases(self, repo: str, etag: str | None = None):
        owner, name = repo.split("/")
        path = f"/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(name)}/releases?per_page={RELEASES_PER_CHECK}"
        return self._request(path, etag)

    def search(self, query: str, limit: int = 8) -> list[dict]:
        q = urllib.parse.quote(query.strip()[:100])
        _, _, data = self._request(f"/search/repositories?q={q}&per_page={limit}")
        return [{"repo": item["full_name"], "description": item.get("description") or "",
                 "stars": item.get("stargazers_count", 0)} for item in (data or {}).get("items", [])]


# ---------------------------------------------------------------------------
# Update checks
# ---------------------------------------------------------------------------

class GitHubReleaseChecker:
    """Update 'provider' for the update service (source id 'github')."""

    id = "github"
    label = "GitHub releases"

    def __init__(self, db: Database, client: GitHubClient | None = None):
        self.db = db
        self.client = client or GitHubClient()

    def available(self) -> bool:
        return True

    def releases_for(self, repo: str) -> list[dict]:
        cached = self.db.github_cache(repo)
        etag = cached[0] if cached else None
        status, new_etag, data = self.client.releases(repo, etag)
        if status == 304 and cached:
            return cached[1]
        releases = _trim(data or [])
        self.db.set_github_cache(repo, new_etag, releases)
        return releases

    def check_updates(self, records: list[AppRecord]) -> list[UpdateInfo]:
        stored = self.db.github_links()
        updates, errors = [], []
        tracked: dict[str, list[AppRecord]] = {}
        for record in records:
            if not is_trackable(record):
                continue
            link = resolve_link(record, stored)
            if link:
                tracked.setdefault(link.repo, []).append(record)
        for repo, apps in tracked.items():
            try:
                releases = self.releases_for(repo)
            except GitHubError as exc:
                errors.append(f"{repo}: {exc}")
                if self.client.rate_remaining == 0:
                    break
                continue
            for record in apps:
                update = self.update_for(record, repo, releases)
                if update:
                    updates.append(update)
        if errors and not updates and len(errors) == len(tracked):
            raise GitHubError("; ".join(errors[:3]))
        return updates

    @staticmethod
    def update_for(record: AppRecord, repo: str, releases: list[dict]) -> UpdateInfo | None:
        kind = install_kind(record)
        release, asset = pick_release(releases, kind)
        if release is None:
            return None
        tag = release.get("tag_name") or release.get("name") or ""
        order = compare_versions(record.version, tag)
        if order is None or order >= 0:
            return None  # up to date, or the installed version is unknown
        latest = ".".join(str(p) for p in version_tuple(tag) or ()) or tag
        return UpdateInfo(
            source="github", package_name=repo, name=record.name, current_version=record.version,
            new_version=latest, download_size=(asset or {}).get("size"), is_application=True,
            origin=f"GitHub: {repo}", app_keys=[record.key],
            extra={"kind": kind, "tag": tag, "release_url": release.get("html_url", ""),
                   "published_at": release.get("published_at", ""), "asset": asset},
        )


def latest_release_summary(db: Database, record: AppRecord, repo: str) -> dict | None:
    """Cached information for the details page (no network access)."""
    cached = db.github_cache(repo)
    if not cached:
        return None
    release, asset = pick_release(cached[1], install_kind(record))
    if release is None:
        return None
    return {"tag": release.get("tag_name", ""), "url": release.get("html_url", ""),
            "published_at": release.get("published_at", ""), "asset": asset, "checked_at": cached[2]}


# ---------------------------------------------------------------------------
# Download and install
# ---------------------------------------------------------------------------

_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,200}$")


def validate_asset(repo: str, asset: dict, kind: str) -> tuple[str, str, int, str | None]:
    """(download URL, file name, size, sha256 or None) after strict checks."""
    url = asset.get("browser_download_url") or ""
    owner, name = repo.split("/")
    prefix = f"https://github.com/{owner}/{name}/releases/download/".lower()
    if not url.lower().startswith(prefix):
        raise GitHubError("The download does not come from this repository's releases.")
    filename = os.path.basename(asset.get("name") or "")
    if not _SAFE_NAME_RE.match(filename):
        raise GitHubError("The release asset has an unexpected file name.")
    if kind == "deb" and not filename.endswith(".deb") or kind == "appimage" and not filename.lower().endswith(".appimage"):
        raise GitHubError("The release asset is not the expected kind of file.")
    size = asset.get("size")
    if not isinstance(size, int) or size <= 0 or size > MAX_DOWNLOAD:
        raise GitHubError("The release asset has an invalid size.")
    digest = asset.get("digest") or ""
    sha256 = digest[7:] if digest.startswith("sha256:") and re.fullmatch(r"[0-9a-f]{64}", digest[7:]) else None
    return url, filename, size, sha256


def download_target(record: AppRecord, kind: str, filename: str) -> str:
    home = home_dir()
    if kind == "appimage":
        directory = os.path.dirname(record.install_path)
        if not directory.startswith(home + "/"):
            directory = os.path.join(home, "Applications")
    else:
        directory = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD) or os.path.join(home, "Downloads")
    target = os.path.join(directory, filename)
    stem, ext = os.path.splitext(filename)
    counter = 1
    while os.path.lexists(target):  # never overwrite (e.g. version-less AppImage names)
        target = os.path.join(directory, f"{stem}-{counter}{ext}")
        counter += 1
    return target


def download(url: str, target: str, size: int, sha256: str | None, log, opener=urllib.request.urlopen) -> str:
    """Stream a release asset to ``target`` (via a .part file), verifying size and digest."""
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if os.path.lexists(target):
        raise RuntimeError(f"{target} already exists; not overwriting it.")
    part = target + ".part"
    digest = hashlib.sha256()
    received = 0
    req = urllib.request.Request(url, headers={"User-Agent": f"hangar/{VERSION}"})
    try:
        with opener(req, timeout=60) as resp, open(part, "wb") as out:
            last = 0.0
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                received += len(chunk)
                if received > size:
                    raise RuntimeError("The download is larger than announced; aborted.")
                digest.update(chunk)
                out.write(chunk)
                if time.monotonic() - last > 0.5:
                    last = time.monotonic()
                    log(f"Downloaded {received * 100 // size}% ({received // 1_000_000} of {size // 1_000_000} MB)")
        if received != size:
            raise RuntimeError("The download is incomplete.")
        if sha256 and digest.hexdigest() != sha256:
            raise RuntimeError("The downloaded file does not match the checksum published by GitHub.")
        os.replace(part, target)
    except BaseException:
        try:
            os.unlink(part)
        except OSError:
            pass
        raise
    log(f"Saved {target}" + (" (checksum verified)" if sha256 else " (no checksum published)"))
    return target


def download_operation(record: AppRecord, update: UpdateInfo, on_downloaded) -> Operation:
    """Download the release asset; ``on_downloaded(path)`` runs on the main loop afterwards."""
    kind = update.extra.get("kind", "any")
    url, filename, size, sha256 = validate_asset(update.package_name, update.extra.get("asset") or {}, kind)
    target = download_target(record, kind, filename)

    def run(log):
        download(url, target, size, sha256, log)
        GLib.idle_add(lambda: on_downloaded(target) or False)

    title = f"Downloading {record.name} {update.new_version}"
    return Operation(title, "download", [record.name], [PythonStep(run, title)], source="GitHub")


def appimage_replace_plan(record: AppRecord, new_path: str) -> dict:
    """What updating an AppImage in place does (no changes made)."""
    if not appimage_type(new_path):
        raise GitHubError("The downloaded file is not an AppImage.")
    old = record.install_path
    home = home_dir()
    launchers = [p for p in record.extra.get("launchers", []) if p.startswith(home + "/") and os.path.isfile(p)]
    links = [p for p in record.extra.get("symlinks", []) if p.startswith(home + "/") and os.path.islink(p)]
    problem = removal_problem(old, home)
    return {"old": old, "new": new_path, "launchers": launchers, "symlinks": links,
            "trash_old": problem is None, "old_problem": problem or ""}


def appimage_replace_operation(record: AppRecord, plan: dict) -> Operation:
    def run(log):
        old, new = plan["old"], plan["new"]
        os.chmod(new, os.lstat(new).st_mode | stat.S_IXUSR | stat.S_IRUSR)
        log("Marked the new AppImage as executable")
        for launcher in plan["launchers"]:
            kf = GLib.KeyFile()
            kf.load_from_file(launcher, GLib.KeyFileFlags.KEEP_COMMENTS | GLib.KeyFileFlags.KEEP_TRANSLATIONS)
            changed = False
            for group in kf.get_groups()[0]:
                for key in ("Exec", "TryExec"):
                    try:
                        value = kf.get_string(group, key)
                    except GLib.Error:
                        continue
                    if old in value:
                        kf.set_string(group, key, value.replace(old, new))
                        changed = True
            if changed:
                tmp = launcher + ".uam-tmp"
                kf.save_to_file(tmp)
                os.replace(tmp, launcher)
                log(f"Updated launcher {launcher}")
        for link in plan["symlinks"]:
            tmp = link + ".uam-tmp"
            os.symlink(new, tmp)
            os.replace(tmp, link)
            log(f"Pointed {link} at the new version")
        if plan["trash_old"] and os.path.lexists(old) and old != new:
            Gio.File.new_for_path(old).trash(None)
            log(f"Moved the old version to the Trash: {old}")

    title = f"Updating {record.name}"
    return Operation(title, "update", [record.name], [PythonStep(run, title)], source="AppImage",
                     app_keys=[record.key])
