"""GitHub release tracking: links, versions, release/asset selection, downloads, AppImage swap."""

import hashlib
import io
import json
import os
import subprocess
import sys
import textwrap

import pytest
from conftest import ROOT, make_appimage, write_desktop

from hangar.core.application_model import AppRecord, Confidence, PackageManager, Source
from hangar.core.database import Database
from hangar.services import github_service as gh


def deb(name="tool", version="1.2.0", homepage="", local=True, vcs=""):
    return AppRecord(key=f"apt:{name}:{name}.desktop", name=name.title(), source=Source.VENDOR,
                     package_manager=PackageManager.APT, confidence=Confidence.MANAGED, package_name=name,
                     version=version, homepage=homepage, extra={"local_deb": local, "vcs": vcs})


def appimage(path="/home/u/Applications/Tool-1.0.0-x86_64.AppImage", update_info=""):
    return AppRecord(key=f"appimage:{path}", name="Tool", source=Source.APPIMAGE,
                     package_manager=PackageManager.NONE, confidence=Confidence.DETECTED,
                     package_name=os.path.basename(path), version="1.0.0", install_path=path, exec_path=path,
                     extra={"update_info": update_info})


def release(tag, assets=(), prerelease=False):
    return {"tag_name": tag, "html_url": f"https://github.com/o/r/releases/tag/{tag}", "prerelease": prerelease,
            "draft": False, "published_at": "2026-01-01T00:00:00Z",
            "assets": [{"name": a, "size": 10, "digest": None,
                        "browser_download_url": f"https://github.com/o/r/releases/download/{tag}/{a}"} for a in assets]}


# -- links ------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,repo", [
    ("vinifmor/bauh", "vinifmor/bauh"),
    ("https://github.com/getagentseal/codeburn", "getagentseal/codeburn"),
    ("https://github.com/owner/repo.git", "owner/repo"),
    ("git@github.com:owner/repo.git", "owner/repo"),
    ("https://github.com/owner/repo/releases/tag/v1", "owner/repo"),
    ("https://github.com/gstride", None),
    ("owner/..", None), ("../etc", None), ("a/b/c", None), ("", None), ("-bad/repo", None),
])
def test_normalize_repo(text, repo):
    assert gh.normalize_repo(text) == repo


def test_links_from_metadata_and_user_decisions():
    image = appimage(update_info="gh-releases-zsync|vinifmor|bauh|latest|bauh-*x86_64.AppImage.zsync")
    assert gh.resolve_link(image, {}).repo == "vinifmor/bauh"
    package = deb(homepage="https://github.com/getagentseal/codeburn")
    link = gh.resolve_link(package, {})
    assert link.repo == "getagentseal/codeburn" and not link.user
    assert gh.resolve_link(deb(vcs="https://github.com/x/y.git"), {}).repo == "x/y"
    org_only = deb(homepage="https://github.com/gstride")
    assert gh.resolve_link(org_only, {}) is None and gh.org_hint(org_only) == "gstride"
    # User decisions win over detection, including turning tracking off.
    assert gh.resolve_link(package, {gh.link_key(package): ("me/fork", "user")}).repo == "me/fork"
    assert gh.resolve_link(package, {gh.link_key(package): ("", "none")}) is None


def test_link_key_survives_appimage_updates():
    old = appimage("/home/u/Applications/Tool-1.0.0-x86_64.AppImage")
    new = appimage("/home/u/Applications/Tool-1.1.0-x86_64.AppImage")
    assert gh.link_key(old) == gh.link_key(new)


def test_only_unmanaged_software_is_tracked():
    assert gh.is_trackable(deb(local=True))
    assert not gh.is_trackable(deb(local=False))      # APT repositories deliver their own updates
    assert gh.is_trackable(appimage())
    web = AppRecord(key="webapp:x", name="X", source=Source.MANUAL, package_manager=PackageManager.NONE,
                    confidence=Confidence.DETECTED, extra={"web_app": True})
    assert not gh.is_trackable(web)


# -- versions and releases -------------------------------------------------------------------

@pytest.mark.parametrize("installed,latest,expected", [
    ("1.2.0", "v1.3.0", -1), ("0.9.27", "desktop-v0.9.27", 0), ("2.0", "1.9.9", 1),
    ("1:2.4.1-1", "v2.5", -1), ("4.97.0-244584", "4.97.1", -1), ("1.2", "1.2.0", 0),
    ("", "v1.0", None), ("1.0", "nightly", None),
])
def test_compare_versions(installed, latest, expected):
    assert gh.compare_versions(installed, latest) == expected


def test_monorepo_release_selection_and_arch_filtering():
    releases = [
        release("mac-v2.0", ["App-2.0.dmg"]),
        release("v2.1-rc1", ["app_2.1_amd64.deb"], prerelease=True),
        release("desktop-v2.0", ["app_2.0_arm64.deb", "app_2.0_amd64.deb", "App-2.0-x86_64.AppImage"]),
    ]
    rel, asset = gh.pick_release(releases, "deb")
    assert rel["tag_name"] == "desktop-v2.0" and asset["name"] == "app_2.0_amd64.deb"
    rel, asset = gh.pick_release(releases, "appimage")
    assert asset["name"] == "App-2.0-x86_64.AppImage"
    rel, asset = gh.pick_release(releases, "any")
    assert rel["tag_name"] == "mac-v2.0" and asset is None


def test_validate_asset_rejects_foreign_or_odd_downloads():
    good = {"name": "app_1.0_amd64.deb", "size": 5, "digest": "sha256:" + "a" * 64,
            "browser_download_url": "https://github.com/o/r/releases/download/v1/app_1.0_amd64.deb"}
    url, name, size, sha = gh.validate_asset("o/r", good, "deb")
    assert name == "app_1.0_amd64.deb" and sha == "a" * 64
    for bad in (
        dict(good, browser_download_url="https://evil.example/app.deb"),
        dict(good, browser_download_url="https://github.com/other/r/releases/download/v1/app.deb"),
        dict(good, name="../../.bashrc"),
        dict(good, name="app.AppImage"),
        dict(good, size=0),
    ):
        with pytest.raises(gh.GitHubError):
            gh.validate_asset("o/r", bad, "deb")


# -- checker with a fake API ----------------------------------------------------------------

class FakeClient:
    def __init__(self, data, etag='W/"1"'):
        self.data, self.etag, self.calls, self.rate_remaining = data, etag, [], 50

    def releases(self, repo, etag=None):
        self.calls.append((repo, etag))
        if etag == self.etag:
            return 304, etag, None
        return 200, self.etag, self.data


def test_checker_reports_newer_release_and_uses_etag_cache():
    db = Database(":memory:")
    record = deb(version="1.2.0", homepage="https://github.com/o/r")
    client = FakeClient([release("v1.3.0", ["tool_1.3.0_amd64.deb"])])
    checker = gh.GitHubReleaseChecker(db, client)
    (update,) = checker.check_updates([record])
    assert update.source == "github" and update.new_version == "1.3.0" and update.app_keys == [record.key]
    assert update.extra["asset"]["name"] == "tool_1.3.0_amd64.deb"
    checker.check_updates([record])
    assert client.calls[-1] == ("o/r", 'W/"1"')       # second check is conditional
    record.version = "1.3.0"
    assert checker.check_updates([record]) == []


def test_checker_raises_when_every_repository_fails():
    class Failing(FakeClient):
        def releases(self, repo, etag=None):
            raise gh.GitHubError("rate limit")

    checker = gh.GitHubReleaseChecker(Database(":memory:"), Failing([]))
    with pytest.raises(gh.GitHubError):
        checker.check_updates([deb(homepage="https://github.com/o/r")])


# -- downloads ------------------------------------------------------------------------------

class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def test_download_verifies_size_and_checksum(tmp_path):
    payload = b"x" * 1000
    good = hashlib.sha256(payload).hexdigest()
    target = tmp_path / "app.deb"
    gh.download("https://github.com/o/r/releases/download/v1/app.deb", str(target), 1000, good, lambda m: None,
                opener=lambda req, timeout: FakeResponse(payload))
    assert target.read_bytes() == payload
    for size, sha in ((1000, "0" * 64), (999, None), (2000, None)):
        bad = tmp_path / f"bad-{size}-{sha is None}.deb"
        with pytest.raises(RuntimeError):
            gh.download("https://github.com/o/r/releases/download/v1/app.deb", str(bad), size, sha, lambda m: None,
                        opener=lambda req, timeout: FakeResponse(payload))
        assert not bad.exists() and not os.path.exists(str(bad) + ".part")


def test_appimage_swap_updates_launcher_and_trashes_old_version(tmp_path):
    home = tmp_path / "home"
    old = make_appimage(str(home / "Applications" / "Tool-1.0.0-x86_64.AppImage"))
    new = make_appimage(str(home / "Applications" / "Tool-1.1.0-x86_64.AppImage"))
    os.chmod(new, 0o644)
    launcher = write_desktop(str(home / ".local/share/applications/tool.desktop"), "Tool", f'"{old}" %U',
                             f"TryExec={old}\n")
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {ROOT!r})
        from hangar.core.application_model import AppRecord, Confidence, PackageManager, Source
        from hangar.services import github_service as gh
        r = AppRecord(key="appimage:{old}", name="Tool", source=Source.APPIMAGE, package_manager=PackageManager.NONE,
                      confidence=Confidence.DETECTED, install_path={old!r}, extra={{"launchers": [{launcher!r}]}})
        plan = gh.appimage_replace_plan(r, {new!r})
        op = gh.appimage_replace_operation(r, plan)
        op.steps[0].func(print)
    """)
    env = dict(os.environ, HOME=str(home), XDG_DATA_HOME=str(home / ".local/share"))
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr
    text = open(launcher).read()
    assert new in text and old not in text
    assert not os.path.exists(old) and os.access(new, os.X_OK)
    assert (home / ".local/share/Trash/files/Tool-1.0.0-x86_64.AppImage").exists()


def test_github_links_persist(tmp_path):
    db = Database(str(tmp_path / "db.sqlite"))
    db.set_github_link("apt:tool", "o/r")
    db.set_github_cache("o/r", 'W/"e"', [release("v1")])
    db2 = Database(str(tmp_path / "db.sqlite"))
    assert db2.github_links() == {"apt:tool": ("o/r", "user")}
    assert db2.github_cache("o/r")[0] == 'W/"e"'
    db2.set_github_link("apt:tool", None)
    assert db2.github_links() == {}
    json.dumps(db2.github_cache("o/r")[1])
