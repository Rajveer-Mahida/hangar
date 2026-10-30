"""Uninstall safety: path policy, helper validation, plans, and Trash execution."""

import json
import os
import subprocess
import sys
import textwrap

import pytest
from conftest import ROOT, make_appimage, write_desktop, write_exec

from hangar import APP_ID
from hangar.core import safety
from hangar.core.application_model import AppRecord, Confidence, PackageManager, Source
from hangar.helper import hangar_helper
from hangar.services import privilege_service, uninstall_service
from hangar.services.operation_service import CommandStep, PythonStep
from hangar.services.uninstall_service import UninstallPlan, build_plan, operations_for


@pytest.fixture(autouse=True)
def fake_pkexec(monkeypatch):
    monkeypatch.setenv("HANGAR_DEV_HELPER", "1")
    monkeypatch.setattr(privilege_service.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(privilege_service, "installed_helper", lambda: None)
    monkeypatch.setattr(uninstall_service, "_dpkg_owner", lambda path: None)
    monkeypatch.setattr(uninstall_service, "_similar_packages", lambda name: [])


# -- path policy ------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "/", "/etc", "/etc/passwd", "/usr", "/usr/bin", "/usr/bin/ls", "/usr/lib/x86_64-linux-gnu",
    "/opt", "/usr/local", "/usr/local/bin", "/usr/share/applications", "/var/lib/dpkg/status",
    "/boot", "/snap/core22", "/var/lib/flatpak/app", "/usr/share/icons", "/home",
])
def test_system_paths_are_protected(path, fake_home):
    assert safety.removal_problem(path, str(fake_home)) is not None
    assert hangar_helper.system_removal_problem(path) is not None


@pytest.mark.parametrize("rel", ["", ".local", ".local/share", ".local/share/applications", ".config",
                                 "Downloads", "Applications", "Desktop", ".ssh", ".gnupg/private",
                                 ".local/share/keyrings"])
def test_home_and_sensitive_paths_are_protected(rel, fake_home):
    target = fake_home / rel
    target.mkdir(parents=True, exist_ok=True)
    assert safety.removal_problem(os.path.normpath(str(target)), str(fake_home)) is not None


@pytest.mark.parametrize("bad", ["relative/path", "/opt/../etc", "/opt//x", "/opt/x/", "/opt/x\n", "/opt/\x00x"])
def test_malformed_paths_rejected(bad, fake_home):
    assert safety.removal_problem(bad, str(fake_home)) is not None
    with pytest.raises(hangar_helper.HelperError):
        hangar_helper.build("remove-paths", [bad])


def test_user_app_folder_is_removable(fake_home):
    app = fake_home / "Downloads" / "Telegram"
    write_exec(str(app / "Telegram"))
    assert safety.removal_problem(str(app), str(fake_home)) is None


def test_symlinked_parent_is_refused(fake_home, tmp_path):
    real = tmp_path / "elsewhere"
    write_exec(str(real / "tool" / "bin"))
    (fake_home / "linkdir").symlink_to(real)
    assert "symbolic link" in safety.removal_problem(str(fake_home / "linkdir" / "tool"), str(fake_home))


def test_owned_paths_are_refused(fake_home):
    target = fake_home / "thing"
    target.mkdir()
    problem = safety.removal_problem(str(target), str(fake_home), owner_lookup=lambda p: "somepkg")
    assert "somepkg" in problem


def test_shared_libraries_refused_outside_home(tmp_path, monkeypatch):
    libdir = tmp_path / "syslib"
    libdir.mkdir()
    (libdir / "libfoo.so.1").write_bytes(b"\x7fELF")
    (libdir / "tool").write_bytes(b"#!/bin/sh\n")
    monkeypatch.setattr(safety, "SYSTEM_REMOVABLE_PARENTS", (str(libdir),))
    home = str(tmp_path / "home")
    assert "shared library" in safety.removal_problem(str(libdir / "libfoo.so.1"), home)
    assert safety.removal_problem(str(libdir / "tool"), home) is None


def test_helper_never_removes_home_files():
    assert "home" in hangar_helper.system_removal_problem("/home/someone/file")


def test_helper_rejects_injection_and_unknown_verbs():
    bad_requests = [
        ("apt-remove", ["-y"]), ("apt-remove", ["foo bar"]), ("apt-remove", ["--expect=ok", "x;y"]),
        ("apt-install", ["--allow-unauthenticated"]), ("snap-remove", ["--devmode", "x"]),
        ("snap-install", ["--dangerous", "x"]), ("flatpak-install", ["remote", "../../x"]),
        ("apt-update", ["extra"]), ("apt-install-deb", ["/etc/passwd"]), ("sh", ["-c", "id"]),
    ]
    for verb, args in bad_requests:
        with pytest.raises((hangar_helper.HelperError, OSError)):
            hangar_helper.build(verb, args)


def test_helper_builds_argument_vectors():
    kind, argv = hangar_helper.build("apt-upgrade", ["firefox", "libfoo:i386"])
    assert kind == "apt-upgrade" and argv[0] == "apt-get" and argv[-2:] == ["firefox", "libfoo:i386"]
    assert "--no-remove" in argv  # upgrades never remove packages
    kind, payload = hangar_helper.build("apt-remove", ["--purge", "--expect=a,b", "a"])
    assert kind == "apt-remove" and payload == (["a"], True, ["a", "b"], [])
    kind, argv = hangar_helper.build("apt-install", ["hello"])
    assert "--no-remove" in argv
    kind, argv = hangar_helper.build("snap-install", ["--classic", "code"])
    assert argv == ["snap", "install", "--classic", "code"]


def test_helper_requires_root():
    if os.geteuid() == 0:
        pytest.skip("running as root")
    assert hangar_helper.main(["hangar-helper", "apt-update"]) == hangar_helper.EXIT_NOT_ROOT


# -- plans ------------------------------------------------------------------------------------

def appimage_record(fake_home):
    path = make_appimage(str(fake_home / "Applications" / "Tool-1.0-x86_64.AppImage"))
    launcher = write_desktop(str(fake_home / ".local/share/applications/tool.desktop"), "Tool", f'"{path}"')
    (fake_home / ".config" / "Tool").mkdir(parents=True)
    return AppRecord(key=f"appimage:{path}", name="Tool", source=Source.APPIMAGE,
                     package_manager=PackageManager.NONE, confidence=Confidence.DETECTED,
                     package_name=os.path.basename(path), install_path=path, exec_path=path,
                     desktop_file=launcher, related_paths=[launcher], extra={"launchers": [launcher]})


def test_appimage_plan_lists_file_launcher_and_optional_data(fake_home):
    record = appimage_record(fake_home)
    plan = build_plan(record, [record])
    kinds = {i.kind: i for i in plan.items}
    assert plan.method == "files" and plan.supported and not plan.requires_root
    assert kinds["file"].selected and kinds["launcher"].selected
    assert kinds["data"].optional and not kinds["data"].selected  # user data is opt-in
    ops = operations_for(plan)
    assert len(ops) == 1 and all(isinstance(s, PythonStep) for s in ops[0].steps)


def test_plan_blocks_protected_targets(fake_home):
    record = AppRecord(key="manual:x", name="Danger", source=Source.MANUAL, package_manager=PackageManager.NONE,
                       confidence=Confidence.UNKNOWN, install_path=str(fake_home))
    plan = build_plan(record, [record])
    assert not plan.supported
    assert all(not i.selected for i in plan.items)
    with pytest.raises(ValueError):
        operations_for(plan)


def test_manual_plan_warns_about_vendor_uninstaller_and_shared_folder(fake_home):
    root = fake_home / "opt-apps" / "Suite"
    write_exec(str(root / "suite"))
    write_exec(str(root / "uninstall.sh"), b"#!/bin/sh\n")
    record = AppRecord(key=f"manual:{root}", name="Suite", source=Source.MANUAL,
                       package_manager=PackageManager.NONE, confidence=Confidence.DETECTED,
                       install_path=str(root), exec_path=str(root / "suite"),
                       extra={"vendor_uninstaller": str(root / "uninstall.sh")})
    other = AppRecord(key="manual:other", name="Plugin", source=Source.MANUAL, package_manager=PackageManager.NONE,
                      confidence=Confidence.DETECTED, exec_path=str(root / "plugins" / "p"))
    plan = build_plan(record, [record, other])
    assert plan.vendor_uninstaller.endswith("uninstall.sh")
    assert any("Plugin" in w for w in plan.warnings)


def test_apt_plan_uses_simulation_and_expect_list(monkeypatch):
    calls = []

    def fake_sim(packages, purge=False, autoremove=False):
        calls.append((tuple(packages), purge, autoremove))
        if autoremove:
            return ["foo", "foo-data", "libold"], [], ""
        return ["foo", "foo-data"], ["foo-replacement"], ""

    monkeypatch.setattr(uninstall_service, "simulate_apt_removal", fake_sim)
    record = AppRecord(key="apt:foo:foo.desktop", name="Foo", source=Source.APT, package_manager=PackageManager.APT,
                       confidence=Confidence.MANAGED, package_name="foo")
    plan = build_plan(record, [record])
    assert plan.affected_packages == ["foo", "foo-data"]
    assert plan.unused_after == ["libold"]
    assert any("foo-data" in w for w in plan.warnings)
    assert any("foo-replacement" in w for w in plan.warnings)  # installs caused by a removal are shown
    (op,) = operations_for(plan)
    (step,) = op.steps
    assert isinstance(step, CommandStep) and step.privileged
    assert "--expect=foo,foo-data" in step.argv and step.argv[-1] == "foo"
    assert "--expect-install=foo-replacement" in step.argv


def test_apt_plan_flags_core_desktop_packages(monkeypatch):
    monkeypatch.setattr(uninstall_service, "simulate_apt_removal",
                        lambda p, purge=False, autoremove=False: (["foo", "ubuntu-desktop", "gdm3"], [], ""))
    record = AppRecord(key="apt:foo:f.desktop", name="Foo", source=Source.APT, package_manager=PackageManager.APT,
                       confidence=Confidence.MANAGED, package_name="foo")
    plan = build_plan(record, [record])
    assert plan.warnings[0].startswith("Warning: this also removes core")


def test_apt_plan_blocks_on_simulation_error(monkeypatch):
    monkeypatch.setattr(uninstall_service, "simulate_apt_removal",
                        lambda p, purge=False, autoremove=False: ([], [], "Essential packages would be removed"))
    record = AppRecord(key="apt:x:x.desktop", name="X", source=Source.APT, package_manager=PackageManager.APT,
                       confidence=Confidence.MANAGED, package_name="x")
    assert not build_plan(record, [record]).supported


def test_snap_base_cannot_be_removed():
    record = AppRecord(key="snap:core22", name="core22", source=Source.SNAP, package_manager=PackageManager.SNAP,
                       confidence=Confidence.MANAGED, package_name="core22", extra={"type": "base"})
    assert not build_plan(record).supported


def test_user_flatpak_removal_needs_no_root(fake_home):
    record = AppRecord(key="flatpak:user:org.x.Y/x86_64/stable", name="Y", source=Source.FLATPAK,
                       package_manager=PackageManager.FLATPAK, confidence=Confidence.MANAGED,
                       package_name="org.x.Y", extra={"ref": "org.x.Y/x86_64/stable", "installation": "user"})
    plan = build_plan(record)
    (op,) = operations_for(plan)
    step = op.steps[0]
    assert "pkexec" not in step.argv[0] and "--user" in step.argv and not step.privileged


def test_system_files_go_through_helper(fake_home):
    record = AppRecord(key="manual:/opt/thing", name="Thing", source=Source.MANUAL,
                       package_manager=PackageManager.NONE, confidence=Confidence.DETECTED)
    plan = UninstallPlan(record=record, method="files", items=[
        uninstall_service.PlanItem("directory", "/opt/thing", requires_root=True)])
    (op,) = operations_for(plan)
    step = op.steps[0]
    assert step.privileged and step.argv[0].endswith("pkexec") and step.argv[-2:] == ["remove-paths", "/opt/thing"]


# -- real execution in an isolated HOME ------------------------------------------------------

def test_trash_execution_end_to_end(tmp_path):
    """Run the actual Trash step in a subprocess whose HOME is a temporary folder."""
    home = tmp_path / "home"
    target = home / "Applications" / "Tool.AppImage"
    make_appimage(str(target))
    keep = home / ".ssh" / "id_test"
    keep.parent.mkdir(parents=True)
    keep.write_text("secret")
    script = textwrap.dedent(f"""
        import json, sys
        sys.path.insert(0, {ROOT!r})
        from hangar.services.uninstall_service import trash_paths
        out = {{"log": []}}
        try:
            trash_paths([{str(target)!r}, {str(keep)!r}], out["log"].append)
            out["error"] = None
        except RuntimeError as exc:
            out["error"] = str(exc)
        print(json.dumps(out))
    """)
    env = dict(os.environ, HOME=str(home), XDG_DATA_HOME=str(home / ".local/share"))
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert not target.exists()
    trashed = list((home / ".local/share/Trash/files").iterdir())
    assert [p.name for p in trashed] == ["Tool.AppImage"]
    assert keep.exists()  # sensitive file refused
    assert result["error"] and "sensitive" in result["error"]


# -- regression tests for the security review -----------------------------------------------

@pytest.mark.parametrize("name", ["pipewire-alsa-", "pulseaudio+", "foo-:amd64"])
def test_apt_action_suffixes_are_rejected(name):
    with pytest.raises(safety.ValidationError):
        safety.validate_apt_package(name)
    with pytest.raises(hangar_helper.HelperError):
        hangar_helper.build("apt-upgrade", [name])


def test_double_plus_package_names_are_allowed():
    assert safety.validate_apt_package("g++") == "g++"
    hangar_helper.build("apt-install", ["g++"])


def test_apt_remove_requires_expectation():
    with pytest.raises(hangar_helper.HelperError):
        hangar_helper.build("apt-remove", ["foo"])


def test_non_image_icon_paths_are_never_offered(fake_home):
    work = fake_home / "Documents" / "work"
    (work / "important").mkdir(parents=True)
    record = appimage_record(fake_home)
    record.related_paths.append(str(work))          # e.g. Icon=/home/u/Documents/work in a launcher
    plan = build_plan(record, [record])
    assert all(i.target != str(work) for i in plan.items)


def test_system_icon_trees_only_accept_image_files():
    assert hangar_helper.system_removal_problem("/usr/share/icons/hicolor/48x48/apps") is not None
    assert "icon" in (hangar_helper.system_removal_problem("/usr/share/icons/hicolor/index.theme") or "icon")


def test_folder_guessed_from_script_is_not_preselected(fake_home):
    root = fake_home / "tools" / "mytool"
    script = write_exec(str(root / "run.sh"), b"#!/bin/sh\n")
    record = AppRecord(key=f"manual:{root}", name="My Tool", source=Source.MANUAL,
                       package_manager=PackageManager.NONE, confidence=Confidence.DETECTED,
                       install_path=str(root), exec_path=script, extra={"root_from_script": True})
    plan = build_plan(record, [record])
    folder = next(i for i in plan.items if i.kind == "directory")
    assert folder.removable and not folder.selected


def test_git_checkouts_and_tool_bin_dirs_are_protected(fake_home):
    repo = fake_home / "src" / "project"
    (repo / ".git").mkdir(parents=True)
    assert "Git" in safety.removal_problem(str(repo), str(fake_home))
    cargo_bin = fake_home / ".cargo" / "bin"
    cargo_bin.mkdir(parents=True)
    assert safety.removal_problem(str(cargo_bin), str(fake_home)) is not None


def test_bind_mounts_below_a_folder_are_detected(fake_home, monkeypatch):
    target = fake_home / "apps" / "thing"
    target.mkdir(parents=True)
    monkeypatch.setattr(safety, "_mount_points", lambda: ["/", str(target / "data")])
    assert "mount point" in safety.removal_problem(str(target), str(fake_home))


def test_helper_copies_and_verifies_installed_files(tmp_path):
    import hashlib

    deb = tmp_path / "pkg.deb"
    deb.write_bytes(b"debian package bytes")
    digest = hashlib.sha256(deb.read_bytes()).hexdigest()
    workdir, copy = hangar_helper.copy_verified(str(deb), digest, ".deb")
    try:
        assert open(copy, "rb").read() == deb.read_bytes() and copy != str(deb)
    finally:
        import shutil

        shutil.rmtree(workdir)
    with pytest.raises(hangar_helper.HelperError) as exc:
        hangar_helper.copy_verified(str(deb), "0" * 64, ".deb")       # file changed after review
    assert exc.value.code == hangar_helper.EXIT_PLAN_CHANGED
    link = tmp_path / "link.deb"
    link.symlink_to(deb)
    with pytest.raises(OSError):
        hangar_helper.copy_verified(str(link), digest, ".deb")        # symlinks are not followed


def test_flatpakref_must_use_https(tmp_path):
    ref = tmp_path / "app.flatpakref"
    ref.write_text("[Flatpak Ref]\nName=org.example.App\nUrl=http://insecure.example/repo\n")
    with pytest.raises(hangar_helper.HelperError):
        hangar_helper.validate_flatpakref(str(ref))
    ref.write_text("[Flatpak Ref]\nName=org.example.App\nUrl=https://dl.example/repo\n")
    hangar_helper.validate_flatpakref(str(ref))


def test_every_helper_verb_has_its_own_polkit_action():
    import re
    import xml.dom.minidom

    doc = xml.dom.minidom.parse(os.path.join(ROOT, "data", f"{APP_ID}.policy.in"))
    actions = {}
    for action in doc.getElementsByTagName("action"):
        annotations = {a.getAttribute("key"): a.firstChild.data for a in action.getElementsByTagName("annotate")}
        active = action.getElementsByTagName("allow_active")[0].firstChild.data
        actions[annotations["org.freedesktop.policykit.exec.argv1"]] = active
    source = open(hangar_helper.__file__).read()
    verbs = set(re.findall(r'verb == "([a-z-]+)"', source)) | {"snap-remove", "snap-install"}
    assert verbs == set(actions)
    kept = {v for v, a in actions.items() if a == "auth_admin_keep"}
    assert kept == {"apt-upgrade", "apt-upgrade-all", "snap-refresh", "flatpak-update", "apt-update"}


def test_history_never_stores_unmatched_command_output():
    from hangar.services.process_service import explain_failure

    tail = ["E: Failed to fetch https://user:token@private.example/repo/Packages 401 Unauthorized"]
    assert "token" not in explain_failure(100, tail, privileged=True, include_output=False)
