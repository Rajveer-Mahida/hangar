"""Where installing an AppImage puts the file and its launcher."""

import os
import shutil
import struct
import subprocess

import pytest
from conftest import make_appimage
from gi.repository import GLib

from hangar.services.install_service import appimage_install_operation, appimage_install_plan


def test_launcher_is_named_after_hangar(fake_home):
    source = make_appimage(str(fake_home / "Downloads" / "Tool-2.0-x86_64.AppImage"))
    plan = appimage_install_plan(source)
    assert plan["target"] == str(fake_home / "Applications" / "Tool-2.0-x86_64.AppImage")
    assert os.path.basename(plan["launcher"]) == "hangar-appimage-tool.desktop"


def test_launcher_from_before_the_rename_is_reused(fake_home):
    # 1.0.1 and older named launchers "uam-appimage-*". Installing the same app again must
    # find that launcher (and refuse to overwrite it) instead of adding a second one.
    apps = fake_home / ".local" / "share" / "applications"
    apps.mkdir(parents=True)
    (apps / "uam-appimage-tool.desktop").write_text("[Desktop Entry]\nType=Application\nName=Tool\n")
    source = make_appimage(str(fake_home / "Downloads" / "Tool-2.0-x86_64.AppImage"))
    assert appimage_install_plan(source)["launcher"] == str(apps / "uam-appimage-tool.desktop")

needs_squashfs = pytest.mark.skipif(not (shutil.which("mksquashfs") and shutil.which("unsquashfs")),
                                    reason="squashfs-tools not installed")

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _real_appimage(path, tmp_path, desktop: str, files: dict[str, bytes]):
    """A type 2 AppImage: ELF header (no sections) followed by a SquashFS image."""
    root = tmp_path / "squashfs-root"
    root.mkdir()
    (root / "app.desktop").write_text(desktop)
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    image = tmp_path / "payload.squashfs"
    subprocess.run(["mksquashfs", str(root), str(image), "-quiet", "-noappend", "-all-root"], check=True,
                   stdout=subprocess.DEVNULL)
    header = bytearray(b"\x7fELF\x02\x01\x01\x00AI\x02" + b"\x00" * 53)
    struct.pack_into("<Q", header, 0x28, 64)       # section headers start right after the header...
    struct.pack_into("<HH", header, 0x3A, 64, 0)   # ...and there are none, so the payload is at 64
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(bytes(header) + image.read_bytes())
    return path


def _install(source):
    plan = appimage_install_plan(source)
    op = appimage_install_operation(plan)
    op.steps[0].func(lambda _line: None)
    kf = GLib.KeyFile()
    kf.load_from_file(plan["launcher"], GLib.KeyFileFlags.NONE)
    return kf


@needs_squashfs
def test_launcher_uses_the_appimage_icon_and_window_class(fake_home, tmp_path):
    desktop = ("[Desktop Entry]\nType=Application\nName=Tool\nIcon=tool\nStartupWMClass=tool-wm\n"
               "Categories=Development;\nComment=Does tool things\n")
    source = _real_appimage(str(fake_home / "Downloads" / "Tool-2.0-x86_64.AppImage"), tmp_path, desktop,
                            {"usr/share/icons/hicolor/256x256/apps/tool.png": PNG, "tool.png": PNG})
    kf = _install(source)
    icon = kf.get_string("Desktop Entry", "Icon")
    assert icon == str(fake_home / ".local" / "share" / "icons" / "hangar-appimage-tool.png")
    with open(icon, "rb") as fh:
        assert fh.read() == PNG
    assert kf.get_string("Desktop Entry", "StartupWMClass") == "tool-wm"
    assert kf.get_string("Desktop Entry", "Categories") == "Development;"
    assert kf.get_string("Desktop Entry", "Comment") == "Does tool things"


@needs_squashfs
def test_unsafe_icon_name_falls_back_to_generic_icon(fake_home, tmp_path):
    desktop = "[Desktop Entry]\nType=Application\nName=Tool\nIcon=../../etc/passwd\n"
    source = _real_appimage(str(fake_home / "Downloads" / "Tool-2.0-x86_64.AppImage"), tmp_path, desktop, {})
    kf = _install(source)
    assert kf.get_string("Desktop Entry", "Icon") == "application-x-executable"


def test_fake_appimage_still_installs_with_generic_icon(fake_home):
    source = make_appimage(str(fake_home / "Downloads" / "Tool-2.0-x86_64.AppImage"))
    kf = _install(source)
    assert kf.get_string("Desktop Entry", "Icon") == "application-x-executable"
    assert "StartupWMClass" not in kf.get_keys("Desktop Entry")[0]
