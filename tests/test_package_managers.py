"""Provider parsing (APT/dpkg, Snap, Flatpak) and process helpers."""

import os

from conftest import write_desktop

from uam.core.application_model import Confidence, PackageManager, Source
from uam.core.desktop_entry import DesktopIndex
from uam.package_managers import apt as apt_module
from uam.package_managers.apt import AptProvider, DpkgDatabase
from uam.package_managers.base import ScanContext
from uam.package_managers.flatpak import FlatpakProvider, parse_list_output, parse_update_output
from uam.package_managers.snap import SnapdClient, SnapProvider
from uam.services import privilege_service
from uam.services.process_service import explain_failure, parse_progress

# -- dpkg / APT --------------------------------------------------------------------

STATUS = """Package: foo
Status: install ok installed
Priority: optional
Section: editors
Installed-Size: 2048
Maintainer: Foo Corp <dev@foo.example>
Architecture: amd64
Version: 1.2.3
Homepage: https://foo.example
Description: The Foo editor
 Foo edits things.
 .
 Second paragraph.

Package: libbar
Status: install ok installed
Priority: required
Architecture: amd64
Multi-Arch: same
Version: 9
Description: bar library

Package: removed-pkg
Status: deinstall ok config-files
Architecture: amd64
Version: 1
Description: gone
"""


def make_dpkg(tmp_path, monkeypatch):
    admin = tmp_path / "dpkg"
    info = admin / "info"
    info.mkdir(parents=True)
    (admin / "status").write_text(STATUS)
    share = tmp_path / "usr/share/applications"
    desktop = write_desktop(str(share / "foo.desktop"), "Foo Editor", str(tmp_path / "opt/foo/bin/foo"),
                            "Comment=Edit all the things\nCategories=Development;\n")
    (info / "foo.list").write_text("\n".join([str(share), desktop, str(tmp_path / "opt/foo"),
                                              str(tmp_path / "opt/foo/bin/foo")]) + "\n")
    (info / "libbar:amd64.list").write_text(str(tmp_path / "usr/lib/libbar.so.9") + "\n")
    logs = tmp_path / "log"
    logs.mkdir()
    (logs / "dpkg.log").write_text("2024-01-02 03:04:05 install foo:amd64 <none> 1.2.3\n")
    monkeypatch.setattr(apt_module, "INDEX_PREFIXES", (str(tmp_path) + "/",))
    monkeypatch.setattr(apt_module, "_NEVER_PACKAGED", ())
    return DpkgDatabase(str(admin), str(logs)).load(), share


def test_dpkg_database_parses_status_lists_and_logs(tmp_path, monkeypatch):
    db, _ = make_dpkg(tmp_path, monkeypatch)
    assert db.native_arch == "amd64"
    assert set(db.packages) == {"foo", "libbar"}  # config-files only packages are skipped
    foo = db.package("foo")
    assert foo.installed_size == 2048 * 1024 and foo.summary == "The Foo editor"
    assert "Second paragraph." in foo.description
    assert db.owner(str(tmp_path / "opt/foo/bin/foo")) == "foo"
    assert db.owner_below(str(tmp_path / "opt/foo")) == "foo"
    assert db.owner_below(str(tmp_path / "opt")) == "foo"
    assert db.owner(str(tmp_path / "usr/lib/libbar.so.9")) == "libbar"  # :amd64 suffix normalised
    assert db.owner(str(tmp_path / "opt/other")) is None
    assert db.install_date("foo") is not None


def test_apt_provider_builds_records(tmp_path, monkeypatch):
    db, share = make_dpkg(tmp_path, monkeypatch)
    ctx = ScanContext(desktop_index=DesktopIndex([str(share)]), dpkg=db, home=str(tmp_path / "home"))
    records = AptProvider().scan(ctx)
    assert len(records) == 1
    r = records[0]
    assert r.package_manager == PackageManager.APT and r.confidence == Confidence.MANAGED
    assert r.name == "Foo Editor" and r.package_name == "foo" and r.version == "1.2.3"
    assert r.source in (Source.APT, Source.VENDOR)
    assert r.summary == "Edit all the things"
    assert ctx.is_claimed(r.desktop_file) and "foo.desktop" in ctx.managed_desktop_ids


# -- Snap ----------------------------------------------------------------------------

class FakeSnapd(SnapdClient):
    def __init__(self, snaps, refresh=None):
        super().__init__(socket_path="/nonexistent")
        self.snaps, self.refresh = snaps, refresh or []

    def get(self, path):
        return self.refresh if "select=refresh" in path else self.snaps

    def get_icon(self, name):
        return None


def test_snap_provider_classifies_apps_and_components(tmp_path):
    desktop = write_desktop(str(tmp_path / "code_code.desktop"), "Visual Studio Code", "/snap/bin/code")
    snaps = [
        {"name": "code", "type": "app", "version": "1.90", "confinement": "classic", "summary": "Editor",
         "install-date": "2024-05-01T10:00:00.123456789+02:00", "installed-size": 1000,
         "publisher": {"display-name": "Microsoft", "validation": "verified"},
         "apps": [{"name": "code", "desktop-file": desktop}]},
        {"name": "core22", "type": "base", "version": "1", "apps": []},
        {"name": "vault", "type": "app", "version": "1.19", "apps": [{"name": "vault"}]},
        {"name": "bad name!", "type": "app"},
    ]
    ctx = ScanContext(desktop_index=DesktopIndex([]))
    records = {r.package_name: r for r in SnapProvider(FakeSnapd(snaps)).scan(ctx)}
    assert set(records) == {"code", "core22", "vault"}
    assert records["code"].has_launcher and not records["code"].is_component
    assert records["code"].install_date is not None
    assert any("Classic confinement" in n for n in records["code"].notes)
    assert records["core22"].is_component
    assert not records["vault"].has_launcher and not records["vault"].is_component
    assert records["code"].origin.startswith("Microsoft")


def test_snap_updates(tmp_path):
    ctx = ScanContext(desktop_index=DesktopIndex([]))
    client = FakeSnapd([{"name": "vault", "type": "app", "version": "1.0", "apps": [{"name": "vault"}]}],
                       refresh=[{"name": "vault", "version": "2.0", "download-size": 42}])
    provider = SnapProvider(client)
    records = provider.scan(ctx)
    updates = provider.check_updates(records)
    assert updates[0].current_version == "1.0" and updates[0].new_version == "2.0"
    assert updates[0].download_size == 42 and updates[0].app_keys == ["snap:vault"]


# -- Flatpak ---------------------------------------------------------------------------

LIST = ("org.example.App\tExample\tAn app\t1.0\tstable\tx86_64\tflathub\tsystem\t12.5 MB\t"
        "org.example.App/x86_64/stable\tsystem,current\n"
        "org.freedesktop.Platform\tFreedesktop Platform\tRuntime\t23.08.1\t23.08\tx86_64\tflathub\tsystem\t"
        "700 MB\torg.freedesktop.Platform/x86_64/23.08\tsystem,runtime,eol=old\n"
        "bad;ref\tBad\t\t\t\t\t\tsystem\t\tbad;ref\tsystem\n")


def test_flatpak_list_parsing():
    rows = parse_list_output(LIST)
    records = FlatpakProvider(binary="/usr/bin/flatpak").records_from_rows(rows, None)
    assert [r.package_name for r in records] == ["org.example.App", "org.freedesktop.Platform"]
    app, runtime = records
    assert app.size == 12_500_000 and not app.is_component
    assert app.extra["ref"] == "org.example.App/x86_64/stable" and app.extra["installation"] == "system"
    assert runtime.is_component and runtime.name.endswith("23.08")
    assert any("End of life" in n for n in runtime.notes)


def test_flatpak_update_parsing():
    rows = parse_update_output("org.example.App\t2.0\tstable\tx86_64\tflathub\t5 MB\tapp/org.example.App/x86_64/stable\n",
                               "user")
    assert rows[0]["ref"].startswith("app/") and rows[0]["installation"] == "user"


# -- process helpers ---------------------------------------------------------------------

def test_parse_progress_apt_status_and_percent():
    fraction, message = parse_progress("pmstatus:firefox:50:Unpacking firefox")
    assert abs(fraction - (0.3 + 0.35)) < 1e-6 and message == "Unpacking firefox"
    fraction, _ = parse_progress("dlstatus:1:50:Retrieving file 1 of 2")
    assert abs(fraction - 0.15) < 1e-6
    assert parse_progress("Installing 2/3… 45%")[0] == 0.45
    assert parse_progress("plain line") == (None, None)


def test_explain_failure_messages():
    assert "cancelled" in explain_failure(126, [], privileged=True)
    assert "not authorized" in explain_failure(127, [], privileged=True)
    assert "Another package manager" in explain_failure(100, ["E: Could not get lock /var/lib/dpkg/lock-frontend"],
                                                        privileged=True)
    assert "exit status 3" in explain_failure(3, ["something odd"], privileged=False)


DPKG_FAILURE = [
    "Setting up google-chrome-stable (156.0.8078.79-1) ...",
    "dpkg: error processing package google-chrome-stable (--configure):",
    " installed google-chrome-stable package post-installation script subprocess returned error exit status 1",
    "dpkg: dependency problems prevent configuration of chrome-addon:",
    " chrome-addon depends on google-chrome-stable; however:",
    "  Package google-chrome-stable is not configured yet.",
    "Setting up brave-origin (1.98.62) ...",
    "Errors were encountered while processing:",
    " google-chrome-stable",
    " chrome-addon",
    "Error: Sub-process /usr/bin/dpkg returned an error code (1)",
]


def test_explain_failure_names_packages_dpkg_could_not_set_up():
    message = explain_failure(100, DPKG_FAILURE, privileged=True)
    assert "google-chrome-stable" in message and "chrome-addon" in message
    assert "post-installation script" in message      # dpkg's reason for the first package
    assert "sudo dpkg --configure -a" in message
    assert "not installed" not in message              # not mistaken for a missing package
    # apt 2.x prints "E:" instead of "Error:"; same result.
    old_apt = [line.replace("Error: ", "E: ") for line in DPKG_FAILURE]
    assert explain_failure(100, old_apt, privileged=True) == message


def test_dpkg_failure_history_message_has_no_output():
    message = explain_failure(100, DPKG_FAILURE, privileged=True, include_output=False)
    assert "google-chrome-stable" not in message
    assert "sudo dpkg --configure -a" in message


SNAP_BUSY = [
    "error: cannot perform the following tasks:",
    '- Make current revision for snap "brave" unavailable (snap "brave" has running apps (brave), pids: 4242)',
    '- Make current revision for snap "thunderbird" unavailable (snap "thunderbird" has running apps '
    '(thunderbird), pids: 5151,5152)',
]


def test_running_snaps_are_named_and_others_not_blamed():
    message = explain_failure(1, SNAP_BUSY, privileged=True)
    assert "brave" in message and "thunderbird" in message
    assert "4242" not in message
    assert "other snaps" in message.lower()
    history = explain_failure(1, SNAP_BUSY, privileged=True, include_output=False)
    assert "brave" not in history and "running" in history


def test_disk_full_wins_over_dpkg_failure():
    tail = ["dpkg: error processing archive /var/cache/apt/archives/foo.deb (--unpack):",
            " cannot copy extracted data for './usr/bin/foo': failed to write (No space left on device)",
            "Errors were encountered while processing:", " /var/cache/apt/archives/foo.deb",
            "E: Sub-process /usr/bin/dpkg returned an error code (1)"]
    assert "disk space" in explain_failure(100, tail, privileged=True)


def test_helper_argv_uses_pkexec_and_argument_arrays(monkeypatch):
    monkeypatch.setenv("UAM_DEV_HELPER", "1")
    monkeypatch.setattr(privilege_service.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(privilege_service, "installed_helper", lambda: None)
    argv = privilege_service.helper_argv("apt-remove", ["--expect=foo", "foo"])
    assert argv[0] == "/usr/bin/pkexec"
    assert argv[-3:] == ["apt-remove", "--expect=foo", "foo"]
    assert all(isinstance(a, str) for a in argv)
    assert os.path.basename(argv[4]) == "uam_helper.py"


def test_development_helper_requires_explicit_opt_in(monkeypatch):
    monkeypatch.delenv("UAM_DEV_HELPER", raising=False)
    monkeypatch.setattr(privilege_service.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(privilege_service, "installed_helper", lambda: None)
    try:
        privilege_service.helper_argv("apt-update", [])
    except privilege_service.PrivilegeError as exc:
        assert "not installed" in str(exc)
    else:
        raise AssertionError("development helper used without opt-in")


def test_helper_argv_validates_before_authentication(monkeypatch):
    monkeypatch.setenv("UAM_DEV_HELPER", "1")
    monkeypatch.setattr(privilege_service.shutil, "which", lambda name: f"/usr/bin/{name}")
    for verb, args in (("apt-remove", ["foo; rm -rf /"]), ("snap-remove", ["$(id)"]),
                       ("flatpak-uninstall", ["../../etc"]), ("remove-paths", ["relative/path"]),
                       ("shell", ["ls"])):
        try:
            privilege_service.helper_argv(verb, args)
        except privilege_service.PrivilegeError:
            continue
        raise AssertionError(f"{verb} {args} was accepted")
