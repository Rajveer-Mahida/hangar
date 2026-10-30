"""Grouping of the same application installed through several sources."""

from hangar.core.application_model import AppRecord, Confidence, PackageManager, Source
from hangar.core.duplicate_detector import assign_groups, identity_tokens, pick_primary


def rec(key, name, source, pm, package="", desktop_id="", exec_path="", **kw):
    return AppRecord(key=key, name=name, source=source, package_manager=pm, confidence=Confidence.MANAGED,
                     package_name=package, desktop_id=desktop_id, exec_path=exec_path, **kw)


def test_same_app_from_three_sources_is_grouped():
    deb = rec("apt:code:code.desktop", "Visual Studio Code", Source.VENDOR, PackageManager.APT, "code",
              "code.desktop")
    snap = rec("snap:code", "Visual Studio Code", Source.SNAP, PackageManager.SNAP, "code", "code_code.desktop")
    flat = rec("flatpak:system:com.visualstudio.code/x86_64/stable", "Code - OSS", Source.FLATPAK,
               PackageManager.FLATPAK, "com.visualstudio.code", "com.visualstudio.code.desktop")
    groups = assign_groups([deb, snap, flat])
    assert len(groups) == 1
    assert deb.group_id == snap.group_id == flat.group_id
    assert pick_primary([snap, flat, deb]) is deb  # package-managed deb preferred


def test_generic_names_do_not_merge_unrelated_apps():
    term = rec("apt:gnome-terminal:org.gnome.Terminal.desktop", "Terminal", Source.APT, PackageManager.APT,
               "gnome-terminal", "org.gnome.Terminal.desktop")
    prefs = rec("apt:gnome-terminal:org.gnome.Terminal.Preferences.desktop", "Terminal Preferences", Source.APT,
                PackageManager.APT, "gnome-terminal", "org.gnome.Terminal.Preferences.desktop")
    other_prefs = rec("apt:gsconnect:org.gnome.Shell.Extensions.GSConnect.Preferences.desktop",
                      "Connect Preferences", Source.APT, PackageManager.APT, "gsconnect",
                      "org.gnome.Shell.Extensions.GSConnect.Preferences.desktop")
    assert assign_groups([term, prefs, other_prefs]) == {}


def test_launchers_of_one_installation_are_not_duplicates():
    a = rec("apt:libreoffice-calc:a.desktop", "Office", Source.APT, PackageManager.APT, "libreoffice-calc", "a.desktop")
    b = rec("apt:libreoffice-calc:b.desktop", "Office", Source.APT, PackageManager.APT, "libreoffice-calc", "b.desktop")
    assert assign_groups([a, b]) == {}
    assert a.group_id == a.key and b.group_id == b.key


def test_hidden_components_and_launchers_are_excluded():
    app = rec("apt:foo:foo.desktop", "Foo", Source.APT, PackageManager.APT, "foo", "foo.desktop")
    hidden = rec("snap:foo", "Foo", Source.SNAP, PackageManager.SNAP, "foo", hidden=True)
    comp = rec("flatpak:system:org.foo.Foo/x86_64/stable", "Foo", Source.FLATPAK, PackageManager.FLATPAK,
               "org.foo.Foo", is_component=True)
    web = rec("webapp:/x/foo.desktop", "Foo", Source.MANUAL, PackageManager.NONE)
    assert assign_groups([app, hidden, comp, web]) == {}


def test_appimage_and_package_share_group():
    deb = rec("apt:obsidian:obsidian.desktop", "Obsidian", Source.VENDOR, PackageManager.APT, "obsidian",
              "obsidian.desktop")
    image = rec("appimage:/home/u/Applications/Obsidian-1.5.AppImage", "Obsidian", Source.APPIMAGE,
                PackageManager.NONE, "Obsidian-1.5.AppImage", exec_path="/home/u/Applications/Obsidian-1.5.AppImage")
    groups = assign_groups([deb, image])
    assert len(groups) == 1 and len(next(iter(groups.values()))) == 2


def test_identity_tokens_ignore_generic_words():
    r = rec("x", "Browser", Source.MANUAL, PackageManager.NONE, exec_path="/opt/x/app")
    assert identity_tokens(r) == set()
