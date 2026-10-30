"""Where installing an AppImage puts the file and its launcher."""

import os

from conftest import make_appimage

from hangar.services.install_service import appimage_install_plan


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
