#!/usr/bin/python3 -I
"""Privileged helper for Hangar.

Executed as root through pkexec (PolicyKit). It accepts a small fixed set of
verbs, validates every argument again (the unprivileged UI is not trusted),
and runs package managers with argument arrays and a clean environment.
It never invokes a shell.

This file is intentionally self-contained: when running as root it must not
import code from locations writable by unprivileged users.

Exit codes: 0 success, 64 invalid request, 65 plan changed since preview,
66 not running as root, other values are the package manager's exit status.
"""

import configparser
import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile

EXIT_INVALID = 64
EXIT_PLAN_CHANGED = 65
EXIT_NOT_ROOT = 66

APT_PACKAGE_RE = re.compile(r"^[a-z0-9][a-z0-9+.\-]{0,127}(:[a-z0-9][a-z0-9\-]{0,31})?$")
SNAP_NAME_RE = re.compile(r"^(?=[a-z0-9-]*[a-z])[a-z0-9](?:-?[a-z0-9]){0,39}(_[a-z0-9]{1,10})?$")
FLATPAK_REF_RE = re.compile(
    r"^(?:(?:app|runtime)/)?[A-Za-z_][A-Za-z0-9_\-]*(?:\.[A-Za-z0-9_\-]+){2,}"
    r"(?:/[A-Za-z0-9_]*(?:/[A-Za-z0-9_.\-]*)?)?$"
)
FLATPAK_REMOTE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,63}$")
SHARED_LIBRARY_RE = re.compile(r"\.so(\.\d+)*$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ICON_EXTENSIONS = (".png", ".svg", ".svgz", ".xpm")

SAFE_ENV = {
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "DEBIAN_FRONTEND": "noninteractive",
    "APT_LISTCHANGES_FRONTEND": "none",
    "NEEDRESTART_MODE": "l",
    "HOME": "/root",
}

APT_COMMON = ["-y", "-o", "DPkg::Lock::Timeout=120", "-o", "APT::Status-Fd=1",
              "-o", "Dpkg::Use-Pty=0", "-o", "Dpkg::Options::=--force-confdef",
              "-o", "Dpkg::Options::=--force-confold"]
# Installs and upgrades must never remove packages as a side effect.
APT_NO_REMOVE = ["--no-remove"]

# --- removal policy (mirrors hangar/core/safety.py; tests keep them in sync) ---
FORBIDDEN_TREES = (
    "/bin", "/boot", "/dev", "/etc", "/lib", "/lib32", "/lib64", "/libx32",
    "/proc", "/root", "/run", "/sbin", "/snap", "/sys", "/srv", "/lost+found",
    "/usr/bin", "/usr/sbin", "/usr/lib", "/usr/lib32", "/usr/lib64", "/usr/libx32",
    "/usr/include", "/usr/libexec", "/usr/src", "/usr/games",
    "/var/lib/dpkg", "/var/lib/apt", "/var/lib/snapd", "/var/lib/flatpak",
    "/var/cache", "/var/log", "/var/spool", "/var/backups",
)
SYSTEM_REMOVABLE_PARENTS = (
    "/opt", "/usr/local/bin", "/usr/local/sbin", "/usr/local/games", "/usr/local/lib",
    "/usr/local/share", "/usr/local/share/applications", "/usr/share/applications", "/usr/share/pixmaps",
)
SYSTEM_ICON_TREES = ("/usr/share/icons/hicolor", "/usr/local/share/icons")
ICON_PARENTS = ("/usr/share/pixmaps",)
FILES_ONLY_PARENTS = (
    "/usr/local/bin", "/usr/local/sbin", "/usr/local/games",
    "/usr/local/share/applications", "/usr/share/applications", "/usr/share/pixmaps",
)
USR_LOCAL_PROTECTED_CHILDREN = {
    "applications", "ca-certificates", "doc", "fonts", "icons", "info", "locale", "man",
    "mime", "pixmaps", "sgml", "xml", "emacs", "zsh", "fish", "vim", "nvim", "bash-completion",
    "node_modules", "pkgconfig", "site_ruby", "lua", "perl", "share", "systemd", "udev", "dbus-1",
    "polkit-1", "glib-2.0", "gir-1.0", "girepository-1.0", "gtk-2.0", "gtk-3.0", "gtk-4.0",
    "gnome-shell", "gstreamer-1.0", "modules", "firmware", "metainfo", "appdata", "themes",
    "sounds", "wayland-sessions", "xsessions", "games", "lib", "libexec", "include", "etc", "bin", "sbin",
}
USR_LOCAL_PROTECTED_RE = re.compile(r"(-linux-gnu[a-z]*|^python[\d.]*|^perl\d*|^ruby|^node|^go$|^gcc|^llvm)", re.I)
PROTECTED = {"/", "/home", "/media", "/mnt", "/opt", "/tmp", "/usr", "/usr/local", "/usr/share",
             "/var", "/var/lib", "/var/tmp", "/usr/share/icons", "/usr/share/icons/hicolor",
             "/usr/local/share/icons"} | set(SYSTEM_REMOVABLE_PARENTS) | set(FORBIDDEN_TREES)


class HelperError(Exception):
    def __init__(self, message, code=EXIT_INVALID):
        super().__init__(message)
        self.code = code


def _within(path, root):
    return path == root or path.startswith(root.rstrip("/") + "/")


def _validate_abs(path):
    if not path or len(path) > 4096 or any(c in path for c in "\x00\n\r"):
        raise HelperError("invalid path")
    if not os.path.isabs(path) or os.path.normpath(path) != path or path.startswith("//"):
        raise HelperError(f"path is not absolute and normalised: {path!r}")


def mount_inside(path):
    """A mount point at or below ``path`` (bind mounts included), if any."""
    prefix = path.rstrip("/") + "/"
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as fh:
            for line in fh:
                fields = line.split()
                if len(fields) > 4:
                    point = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), fields[4])
                    if point == path or point.startswith(prefix):
                        return point
    except OSError:
        return "unknown (mount table unreadable)"
    return None


def dpkg_owner_below(path):
    """Return a package owning ``path`` or anything below it (reads dpkg lists)."""
    prefix = path.rstrip("/") + "/"
    info = "/var/lib/dpkg/info"
    try:
        names = os.listdir(info)
    except OSError:
        return None
    for name in names:
        if not name.endswith(".list"):
            continue
        try:
            with open(os.path.join(info, name), encoding="utf-8", errors="surrogateescape") as fh:
                for line in fh:
                    owned = line.rstrip("\n")
                    if owned == path or owned.startswith(prefix):
                        return name[:-5]
        except OSError:
            continue
    return None


def system_removal_problem(path):
    """Policy for root-owned locations. User files are never removed as root."""
    _validate_abs(path)
    if _within(path, "/home") or _within(path, "/root"):
        return "files in home directories are removed by the user session, not as root"
    if path in PROTECTED:
        return "protected system directory"
    for tree in FORBIDDEN_TREES:
        if _within(path, tree):
            return f"inside protected tree {tree}"
    parent = os.path.dirname(path)
    if os.path.realpath(parent) != parent:
        return "reached through a symbolic link"
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return "does not exist"
    is_file_or_link = stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)
    if any(_within(path, t) and path != t for t in SYSTEM_ICON_TREES) or parent in ICON_PARENTS:
        if not (stat.S_ISREG(st.st_mode) and path.lower().endswith(ICON_EXTENSIONS)):
            return "only icon image files are removed from icon folders"
    elif parent not in SYSTEM_REMOVABLE_PARENTS:
        return "outside the locations that may be cleaned up"
    base = os.path.basename(path)
    if parent in ("/usr/local/lib", "/usr/local/share") and (
            base in USR_LOCAL_PROTECTED_CHILDREN or USR_LOCAL_PROTECTED_RE.search(base)):
        return "shared system directory"
    if is_file_or_link and SHARED_LIBRARY_RE.search(base):
        return "shared library"
    if parent in FILES_ONLY_PARENTS and not is_file_or_link:
        return "directories are not removed from this location"
    if parent in ("/usr/share/applications", "/usr/local/share/applications") and not path.endswith(".desktop"):
        return "not a desktop launcher"
    if stat.S_ISDIR(st.st_mode):
        if os.lstat(parent).st_dev != st.st_dev:
            return "mount point"
        mount = mount_inside(path)
        if mount:
            return f"contains a mount point ({mount})"
        for root, dirs, _files in os.walk(path, followlinks=False):
            for d in dirs:
                try:
                    if os.lstat(os.path.join(root, d)).st_dev != st.st_dev:
                        return "contains a mount point"
                except OSError:
                    continue
    elif not is_file_or_link:
        return "not a regular file, link or directory"
    owner = dpkg_owner_below(path)
    if owner:
        return f"owned by the package {owner}"
    return None


def _apt_name(name):
    """Validate one package name. APT treats a trailing '-' as 'remove' and a
    trailing '+' as 'install', so such suffixes are refused (except '++' names
    such as g++)."""
    if not APT_PACKAGE_RE.match(name):
        raise HelperError(f"invalid package name: {name!r}")
    bare = name.split(":", 1)[0]
    if bare.endswith("-") or (bare.endswith("+") and not bare.endswith("++")):
        raise HelperError(f"package names with APT action suffixes are not accepted: {name!r}")
    return name


def _apt_packages(args):
    if not args:
        raise HelperError("no packages given")
    return [_apt_name(a) for a in args]


def _apt_list(value):
    return [_apt_name(x) for x in value.split(",") if x]


def apt_simulation(action, packages):
    """(removed, installed) package sets from an apt-get dry run."""
    cmd = ["apt-get", "-s", action, *packages]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=SAFE_ENV, stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        raise HelperError("apt-get simulation failed: " + (proc.stderr.strip().splitlines() or ["?"])[-1])
    removed, installed = set(), set()
    for line in proc.stdout.splitlines():
        if line.startswith(("Remv ", "Purg ")):
            removed.add(line.split()[1])
        elif line.startswith("Inst "):
            installed.add(line.split()[1])
    return removed, installed


def require_installed(packages):
    """Upgrades and removals only make sense for packages that are installed (exact names)."""
    proc = subprocess.run(["dpkg-query", "-W", "-f=${binary:Package} ${db:Status-Status}\\n", *packages],
                          capture_output=True, text=True, env=SAFE_ENV, stdin=subprocess.DEVNULL)
    installed = set()
    for line in proc.stdout.splitlines():
        name, _, status = line.partition(" ")
        if status == "installed":
            installed.add(name)
            installed.add(name.split(":", 1)[0])
    missing = [p for p in packages if p not in installed]
    if missing:
        raise HelperError("not installed: " + ", ".join(missing))


def copy_verified(path, sha256, suffix):
    """Copy a user-supplied file into a root-only directory and verify its hash.

    The file the user reviewed is identified by its SHA-256; the package
    manager only ever sees the private copy, so swapping the original after
    the preview has no effect.
    """
    _validate_abs(path)
    if not SHA256_RE.match(sha256 or ""):
        raise HelperError("missing or invalid --sha256")
    if not path.endswith(suffix):
        raise HelperError(f"expected a {suffix} file")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise HelperError("not a regular file")
        if st.st_size > 4 * 1024 ** 3:
            raise HelperError("file is too large")
        workdir = tempfile.mkdtemp(prefix="hangar-helper-", dir="/var/tmp")
        os.chmod(workdir, 0o700)
        target = os.path.join(workdir, "package" + suffix)
        digest = hashlib.sha256()
        try:
            with os.fdopen(os.dup(fd), "rb") as src, open(target, "xb") as dst:
                for chunk in iter(lambda: src.read(1024 * 1024), b""):
                    digest.update(chunk)
                    dst.write(chunk)
        except OSError:
            shutil.rmtree(workdir, ignore_errors=True)
            raise
    finally:
        os.close(fd)
    if digest.hexdigest() != sha256:
        shutil.rmtree(workdir, ignore_errors=True)
        raise HelperError("the file changed after it was reviewed; nothing was installed", EXIT_PLAN_CHANGED)
    os.chmod(target, 0o644)
    return workdir, target


def validate_flatpakref(path):
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with open(path, encoding="utf-8") as fh:
            parser.read_file(fh)
    except (OSError, configparser.Error, UnicodeDecodeError) as exc:
        raise HelperError(f"invalid .flatpakref: {exc}")
    if "Flatpak Ref" not in parser:
        raise HelperError("invalid .flatpakref: missing [Flatpak Ref]")
    ref = parser["Flatpak Ref"]
    if not FLATPAK_REF_RE.match(ref.get("Name", "")):
        raise HelperError("invalid application ID in .flatpakref")
    for key in ("Url", "RuntimeRepo"):
        value = ref.get(key, "")
        if value and not value.startswith("https://"):
            raise HelperError(f"{key} must use https")
    if not ref.get("Url"):
        raise HelperError(".flatpakref has no repository URL")


def _split_flags(args, allowed_prefixes):
    flags, rest = {}, []
    for a in args:
        if a.startswith("--"):
            key, eq, value = a.partition("=")
            if key not in allowed_prefixes:
                raise HelperError(f"unknown option {key!r}")
            flags[key] = value if eq else True
        else:
            rest.append(a)
    return flags, rest


def build(verb, args):
    """Return ('cmd', argv) or ('remove-paths', [paths]) for a verb. Pure validation."""
    if verb == "apt-remove":
        flags, rest = _split_flags(args, {"--purge", "--expect", "--expect-install"})
        if "--expect" not in flags or flags["--expect"] is True:
            raise HelperError("apt-remove requires --expect=<packages shown in the preview>")
        expect = _apt_list(flags["--expect"])
        expect_install = flags.get("--expect-install", "")
        expect_install = _apt_list(expect_install) if isinstance(expect_install, str) else []
        return ("apt-remove", (_apt_packages(rest), bool(flags.get("--purge")), expect, expect_install))
    if verb == "apt-upgrade":
        pkgs = _apt_packages(args)
        return ("apt-upgrade", ["apt-get", *APT_COMMON, *APT_NO_REMOVE, "install", "--only-upgrade", *pkgs])
    if verb == "apt-upgrade-all":
        if args:
            raise HelperError("apt-upgrade-all takes no arguments")
        return ("cmd", ["apt-get", *APT_COMMON, "upgrade"])
    if verb == "apt-install":
        pkgs = _apt_packages(args)
        return ("cmd", ["apt-get", *APT_COMMON, *APT_NO_REMOVE, "install", *pkgs])
    if verb == "apt-install-deb":
        flags, rest = _split_flags(args, {"--sha256"})
        if len(rest) != 1 or not isinstance(flags.get("--sha256"), str) or not SHA256_RE.match(flags["--sha256"]):
            raise HelperError("apt-install-deb takes --sha256=<hash> and one .deb file")
        _validate_abs(rest[0])
        if not rest[0].endswith(".deb"):
            raise HelperError("not a .deb file")
        return ("install-file", (".deb", rest[0], flags["--sha256"]))
    if verb == "apt-update":
        if args:
            raise HelperError("apt-update takes no arguments")
        return ("cmd", ["apt-get", "-o", "APT::Status-Fd=1", "update"])
    if verb in ("snap-remove", "snap-install"):
        flags = [a for a in args if a.startswith("--")]
        names = [a for a in args if not a.startswith("--")]
        allowed = {"--purge"} if verb == "snap-remove" else {"--classic"}
        if len(names) != 1 or any(f not in allowed for f in flags) or not SNAP_NAME_RE.match(names[0]):
            raise HelperError("invalid snap request")
        return ("cmd", ["snap", "remove" if verb == "snap-remove" else "install", *flags, names[0]])
    if verb == "snap-refresh":
        for a in args:
            if not SNAP_NAME_RE.match(a):
                raise HelperError(f"invalid snap name: {a!r}")
        return ("cmd", ["snap", "refresh", *args])
    if verb == "flatpak-uninstall":
        if len(args) != 1 or not FLATPAK_REF_RE.match(args[0]):
            raise HelperError("invalid flatpak ref")
        return ("cmd", ["flatpak", "uninstall", "--system", "-y", "--noninteractive", args[0]])
    if verb == "flatpak-update":
        for a in args:
            if not FLATPAK_REF_RE.match(a):
                raise HelperError(f"invalid flatpak ref: {a!r}")
        return ("cmd", ["flatpak", "update", "--system", "-y", "--noninteractive", *args])
    if verb == "flatpak-remove-unused":
        if args:
            raise HelperError("flatpak-remove-unused takes no arguments")
        return ("cmd", ["flatpak", "uninstall", "--system", "--unused", "-y", "--noninteractive"])
    if verb == "flatpak-install":
        if len(args) != 2 or not FLATPAK_REMOTE_RE.match(args[0]) or not FLATPAK_REF_RE.match(args[1]):
            raise HelperError("invalid flatpak install request")
        return ("cmd", ["flatpak", "install", "--system", "-y", "--noninteractive", args[0], args[1]])
    if verb == "flatpak-install-ref":
        flags, rest = _split_flags(args, {"--sha256"})
        if len(rest) != 1 or not isinstance(flags.get("--sha256"), str) or not SHA256_RE.match(flags["--sha256"]):
            raise HelperError("flatpak-install-ref takes --sha256=<hash> and one .flatpakref file")
        _validate_abs(rest[0])
        if not rest[0].endswith(".flatpakref"):
            raise HelperError("not a .flatpakref file")
        return ("install-file", (".flatpakref", rest[0], flags["--sha256"]))
    if verb == "remove-paths":
        if not args or len(args) > 200:
            raise HelperError("invalid number of paths")
        for p in args:
            _validate_abs(p)
        return ("remove-paths", list(args))
    raise HelperError(f"unknown verb: {verb!r}")


def run_command(argv):
    binary = shutil.which(argv[0], path=SAFE_ENV["PATH"])
    if not binary:
        raise HelperError(f"{argv[0]} is not installed")
    proc = subprocess.Popen([binary, *argv[1:]], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, env=SAFE_ENV, close_fds=True)
    for raw in proc.stdout:
        sys.stdout.write(raw.decode("utf-8", "replace"))
        sys.stdout.flush()
    return proc.wait()


def remove_paths(paths):
    problems = {p: system_removal_problem(p) for p in paths}
    refused = {p: why for p, why in problems.items() if why}
    if refused:
        for p, why in refused.items():
            print(f"REFUSED: {p}: {why}")
        raise HelperError("some paths may not be removed; nothing was changed")
    if not shutil.rmtree.avoids_symlink_attacks:
        raise HelperError("safe recursive removal is not available on this system")
    failed = False
    for p in paths:
        try:
            st = os.lstat(p)
            if stat.S_ISDIR(st.st_mode):
                shutil.rmtree(p)
            else:
                os.unlink(p)
            print(f"Removed: {p}")
        except OSError as exc:
            failed = True
            print(f"FAILED: {p}: {exc.strerror}")
        sys.stdout.flush()
    return 1 if failed else 0


def main(argv):
    if os.geteuid() != 0:
        print("ERROR: the helper must be run through pkexec", file=sys.stderr)
        return EXIT_NOT_ROOT
    if len(argv) < 2:
        print("ERROR: missing verb", file=sys.stderr)
        return EXIT_INVALID
    os.umask(0o022)
    os.chdir("/")
    try:
        kind, payload = build(argv[1], argv[2:])
        if kind == "cmd":
            return run_command(payload)
        if kind == "remove-paths":
            return remove_paths(payload)
        if kind == "apt-upgrade":
            require_installed(payload[payload.index("--only-upgrade") + 1:])
            return run_command(payload)
        if kind == "apt-remove":
            pkgs, purge, expect, expect_install = payload
            require_installed(pkgs)
            removed, installed = apt_simulation("purge" if purge else "remove", pkgs)
            if removed != set(expect) or installed != set(expect_install):
                print("ERROR: the packages that would be removed changed since the preview: "
                      + ", ".join(sorted(removed)) + ("; installed: " + ", ".join(sorted(installed))
                                                      if installed else ""))
                return EXIT_PLAN_CHANGED
            return run_command(["apt-get", *APT_COMMON, "purge" if purge else "remove", *pkgs])
        if kind == "install-file":
            suffix, path, sha256 = payload
            workdir, copy = copy_verified(path, sha256, suffix)
            try:
                if suffix == ".deb":
                    return run_command(["apt-get", *APT_COMMON, *APT_NO_REMOVE, "install", copy])
                validate_flatpakref(copy)
                return run_command(["flatpak", "install", "--system", "-y", "--noninteractive", "--from", copy])
            finally:
                shutil.rmtree(workdir, ignore_errors=True)
    except HelperError as exc:
        print(f"ERROR: {exc}")
        return exc.code
    except OSError as exc:
        print(f"ERROR: {exc.strerror or exc}")
        return EXIT_INVALID
    return EXIT_INVALID


if __name__ == "__main__":
    sys.exit(main(sys.argv))
