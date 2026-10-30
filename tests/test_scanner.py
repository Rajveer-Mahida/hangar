"""Discovery: desktop entries, AppImages and the manual software scanner."""

import os

from conftest import make_appimage, write_desktop, write_exec

from hangar.core.application_model import Confidence, PackageManager, Source
from hangar.core.desktop_entry import DesktopIndex, parse_desktop_file, resolve_program, split_exec
from hangar.core.scanner import Scanner
from hangar.core.database import Database
from hangar.core.settings import Settings
from hangar.package_managers.appimage import AppImageProvider, appimage_type, name_and_version, read_update_info
from hangar.package_managers.base import ScanContext
from hangar.package_managers.manual import ManualProvider, install_root


# -- desktop entries -----------------------------------------------------------

def test_split_exec_drops_field_codes_and_respects_quotes():
    assert split_exec('"/opt/My App/app" --flag %U') == ["/opt/My App/app", "--flag"]
    assert split_exec("foo %f %% %i") == ["foo"]


def test_resolve_program_skips_env_and_interpreters(tmp_path):
    script = write_exec(str(tmp_path / "tool.py"), b"#!/usr/bin/python3\n")
    token, target, args = resolve_program(["env", "GDK_BACKEND=x11", "/usr/bin/python3", "-u", script, "--x"])
    assert target == script and args == ["--x"]
    token, target, _ = resolve_program(["sh", "-c", "echo hi"])
    assert target == ""  # shell snippets are never resolved (and never run)


def test_parse_desktop_file_reads_main_group_only(tmp_path):
    path = write_desktop(str(tmp_path / "a.desktop"), "Alpha", "/opt/alpha/alpha %U",
                         "NoDisplay=true\nCategories=Development;IDE;\n"
                         "[Desktop Action New]\nName=New\nExec=/somewhere/else\n")
    entry = parse_desktop_file(path)
    assert entry.name == "Alpha"
    assert entry.exec_target == "/opt/alpha/alpha"
    assert entry.no_display and not entry.visible
    assert entry.categories == ["Development", "IDE"]


def test_parse_rejects_invalid_files(tmp_path):
    bad = tmp_path / "bad.desktop"
    bad.write_text("not a key file")
    assert parse_desktop_file(str(bad)) is None
    assert parse_desktop_file(str(tmp_path / "missing.desktop")) is None


def test_web_app_detection(tmp_path):
    path = write_desktop(str(tmp_path / "pwa.desktop"), "Teams",
                         "/opt/brave.com/brave/brave --profile-directory=Default --app-id=abcdef %U")
    assert parse_desktop_file(path).web_app_id == "abcdef"


# -- AppImages -------------------------------------------------------------------

def test_appimage_magic_detection(tmp_path):
    real = make_appimage(str(tmp_path / "Tool-1.2.3-x86_64.AppImage"))
    fake = tmp_path / "Fake.AppImage"
    fake.write_bytes(b"#!/bin/sh\necho not an appimage\n")
    assert appimage_type(real) == 2
    assert appimage_type(str(fake)) is None
    link = tmp_path / "link.AppImage"
    link.symlink_to(real)
    assert appimage_type(str(link)) is None  # symlinks are not followed


def test_appimage_update_info_is_read_without_execution(tmp_path):
    info = "gh-releases-zsync|owner|repo|latest|Tool-*x86_64.AppImage.zsync"
    path = make_appimage(str(tmp_path / "Tool.AppImage"), update_info=info)
    assert read_update_info(path) == info


def test_name_and_version_from_filename():
    assert name_and_version("requestly-api-client-2608.11.1-linux-x86_64.AppImage") == (
        "Requestly Api Client", "2608.11.1")
    assert name_and_version("Obsidian-1.5.3.AppImage") == ("Obsidian", "1.5.3")
    assert name_and_version("bauh-0.10.7-x86_64.AppImage") == ("Bauh", "0.10.7")


def test_appimage_provider_links_launcher(tmp_path, fake_home):
    apps = fake_home / "Applications"
    image = make_appimage(str(apps / "Tool-2.0-x86_64.AppImage"))
    launcher = write_desktop(str(fake_home / ".local/share/applications/tool.desktop"), "Tool Deluxe",
                             f'"{image}" %U', "Icon=tool\n")
    ctx = ScanContext(desktop_index=DesktopIndex([str(fake_home / ".local/share/applications")]),
                      home=str(fake_home), appimage_dirs=[str(apps)])
    records = AppImageProvider().scan(ctx)
    assert len(records) == 1
    r = records[0]
    assert r.name == "Tool Deluxe" and r.version == "2.0"
    assert r.source == Source.APPIMAGE and r.confidence == Confidence.DETECTED
    assert launcher in r.related_paths
    assert ctx.is_claimed(launcher) and ctx.is_claimed(image)


# -- manual discovery --------------------------------------------------------------

def _manual_fixture(tmp_path, fake_home):
    opt = tmp_path / "opt"
    write_exec(str(opt / "FooApp" / "fooapp"))
    write_exec(str(opt / "FooApp" / "uninstall.sh"), b"#!/bin/sh\n")
    (opt / "EmptyThing").mkdir(parents=True)
    bins = tmp_path / "bin"
    bins.mkdir()
    (bins / "foo").symlink_to(opt / "FooApp" / "fooapp")
    (bins / "gone").symlink_to(tmp_path / "does-not-exist")
    write_exec(str(bins / "clitool"))
    user_apps = fake_home / ".local/share/applications"
    bar = write_exec(str(fake_home / "tools/Bar/bin/bar"))
    write_desktop(str(user_apps / "bar.desktop"), "Bar Editor", f"{bar} %F")
    write_desktop(str(user_apps / "foo.desktop"), "Foo App", str(opt / "FooApp" / "fooapp"))
    write_desktop(str(user_apps / "broken.desktop"), "Ghost", str(fake_home / "deleted/ghost"))
    write_desktop(str(user_apps / "pwa.desktop"), "Web Thing", "/opt/x/chrome --app-id=zzz")
    return opt, bins, user_apps


def test_manual_provider_classifies_items(tmp_path, fake_home):
    opt, bins, user_apps = _manual_fixture(tmp_path, fake_home)
    ctx = ScanContext(desktop_index=DesktopIndex([str(user_apps)]), home=str(fake_home))
    records = {r.key: r for r in ManualProvider(opt_dir=str(opt), bin_dirs=(str(bins),)).scan(ctx)}

    foo = records[f"manual:{opt}/FooApp"]
    assert foo.name == "Foo App" and foo.confidence == Confidence.DETECTED
    assert foo.extra["vendor_uninstaller"].endswith("uninstall.sh")
    assert str(bins / "foo") in foo.extra["symlinks"]
    assert foo.package_manager == PackageManager.NONE

    bar = records[f"manual:{fake_home}/tools/Bar"]  # bin/ is stripped to find the install root
    assert bar.name == "Bar Editor" and bar.source == Source.MANUAL

    ghost = next(r for r in records.values() if r.name == "Ghost")
    assert ghost.extra.get("broken_launcher") and ghost.confidence == Confidence.UNKNOWN

    web = next(r for r in records.values() if r.name == "Web Thing")
    assert web.extra.get("web_app")

    cli = records[f"manual:{bins}/clitool"]
    assert cli.confidence == Confidence.UNKNOWN  # an executable alone is not assumed to be an app
    assert not cli.has_launcher

    gone = records[f"link:{bins}/gone"]
    assert gone.extra.get("broken_link")

    empty = records[f"manual:{opt}/EmptyThing"]
    assert empty.confidence == Confidence.UNKNOWN


def test_install_root_never_returns_boundaries(fake_home):
    home = str(fake_home)
    assert install_root(f"{home}/.local/bin/tool", home) == f"{home}/.local/bin/tool"
    assert install_root(f"{home}/Downloads/App/app", home) == f"{home}/Downloads/App"
    assert install_root("/opt/vendor/sub/bin/app", home) == "/opt/vendor"
    assert install_root("/usr/local/bin/x", home) == "/usr/local/bin/x"


def _tree_state(root):
    state = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = os.path.join(dirpath, name)
            st = os.lstat(p)
            state[p] = (st.st_mode, st.st_size, st.st_mtime_ns)
    return state


def test_full_scan_does_not_modify_filesystem(tmp_path, fake_home, monkeypatch):
    opt, bins, user_apps = _manual_fixture(tmp_path, fake_home)
    make_appimage(str(fake_home / "Applications/Thing.AppImage"))
    monkeypatch.setenv("XDG_DATA_HOME", str(fake_home / ".local/share"))
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path / "share"))
    before = _tree_state(tmp_path)
    db = Database(":memory:")
    settings = Settings.load(db)
    settings.application_dirs = [str(fake_home / "Applications")]
    scanner = Scanner(settings, providers=[AppImageProvider(), ManualProvider(str(opt), (str(bins),))])
    result = scanner.scan()
    assert result.provider_status == {"appimage": "ok", "manual": "ok"}
    assert any(r.source == Source.APPIMAGE for r in result.records)
    assert _tree_state(tmp_path) == before


def test_disabled_provider_is_skipped(tmp_path):
    db = Database(":memory:")
    settings = Settings.load(db)
    settings.set_provider("manual", False)
    result = Scanner(settings, providers=[ManualProvider(str(tmp_path), ())]).scan()
    assert result.provider_status["manual"] == "disabled"
    assert result.records == []
