"""Validation of package identifiers and filesystem paths.

Everything that ends up as an argument to a package manager or as a removal
target goes through this module first. The privileged helper re-implements
the same checks independently (it must not import code from user-writable
locations), and tests assert that both agree.
"""

from __future__ import annotations

import os
import re
import stat

APT_PACKAGE_RE = re.compile(r"^[a-z0-9][a-z0-9+.\-]{0,127}(:[a-z0-9][a-z0-9\-]{0,31})?$")
SNAP_NAME_RE = re.compile(r"^(?=[a-z0-9-]*[a-z])[a-z0-9](?:-?[a-z0-9]){0,39}(_[a-z0-9]{1,10})?$")
FLATPAK_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-]*(\.[A-Za-z0-9_\-]+){2,}$")
FLATPAK_REF_RE = re.compile(
    r"^(?:(?:app|runtime)/)?[A-Za-z_][A-Za-z0-9_\-]*(?:\.[A-Za-z0-9_\-]+){2,}"
    r"(?:/[A-Za-z0-9_]*(?:/[A-Za-z0-9_.\-]*)?)?$"
)
FLATPAK_REMOTE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,63}$")
FLATPAK_INSTALLATIONS = ("system", "user")

MAX_PATH = 4096
SHARED_LIBRARY_RE = re.compile(r"\.so(\.\d+)*$")


class ValidationError(ValueError):
    """Raised when an identifier or path is unsafe to act on."""


def validate_apt_package(name: str) -> str:
    if not isinstance(name, str) or not APT_PACKAGE_RE.match(name):
        raise ValidationError(f"Invalid APT package name: {name!r}")
    bare = name.split(":", 1)[0]
    # APT reads a trailing '-' as "remove" and '+' as "install" (g++ style names are fine).
    if bare.endswith("-") or (bare.endswith("+") and not bare.endswith("++")):
        raise ValidationError(f"Package name with an APT action suffix: {name!r}")
    return name


def validate_snap_name(name: str) -> str:
    if not isinstance(name, str) or not SNAP_NAME_RE.match(name):
        raise ValidationError(f"Invalid snap name: {name!r}")
    return name


def validate_flatpak_ref(ref: str) -> str:
    if not isinstance(ref, str) or len(ref) > 255 or not FLATPAK_REF_RE.match(ref):
        raise ValidationError(f"Invalid Flatpak reference: {ref!r}")
    return ref


def validate_flatpak_id(app_id: str) -> str:
    if not isinstance(app_id, str) or len(app_id) > 255 or not FLATPAK_ID_RE.match(app_id):
        raise ValidationError(f"Invalid Flatpak application ID: {app_id!r}")
    return app_id


def validate_flatpak_remote(remote: str) -> str:
    if not isinstance(remote, str) or not FLATPAK_REMOTE_RE.match(remote):
        raise ValidationError(f"Invalid Flatpak remote: {remote!r}")
    return remote


def validate_flatpak_installation(installation: str) -> str:
    if installation not in FLATPAK_INSTALLATIONS:
        raise ValidationError(f"Unsupported Flatpak installation: {installation!r}")
    return installation


def validate_abs_path(path: str) -> str:
    """Require a clean, absolute, normalised path."""
    if not isinstance(path, str) or not path:
        raise ValidationError("Empty path")
    if len(path) > MAX_PATH or "\x00" in path or "\n" in path or "\r" in path:
        raise ValidationError(f"Invalid characters in path: {path!r}")
    if not os.path.isabs(path):
        raise ValidationError(f"Path is not absolute: {path!r}")
    if os.path.normpath(path) != path or path.startswith("//"):
        raise ValidationError(f"Path is not normalised: {path!r}")
    return path


# ---------------------------------------------------------------------------
# Removal policy
# ---------------------------------------------------------------------------

# Never removed, never removed *through* (anything below them is off limits).
FORBIDDEN_TREES = (
    "/bin", "/boot", "/dev", "/etc", "/lib", "/lib32", "/lib64", "/libx32",
    "/proc", "/root", "/run", "/sbin", "/snap", "/sys", "/srv", "/lost+found",
    "/usr/bin", "/usr/sbin", "/usr/lib", "/usr/lib32", "/usr/lib64", "/usr/libx32",
    "/usr/include", "/usr/libexec", "/usr/src", "/usr/games",
    "/var/lib/dpkg", "/var/lib/apt", "/var/lib/snapd", "/var/lib/flatpak",
    "/var/cache", "/var/log", "/var/spool", "/var/backups",
)

# System locations where *children* of these directories may be removed after
# all other checks pass. The directories themselves are protected.
SYSTEM_REMOVABLE_PARENTS = (
    "/opt",
    "/usr/local/bin",
    "/usr/local/sbin",
    "/usr/local/games",
    "/usr/local/lib",
    "/usr/local/share",
    "/usr/local/share/applications",
    "/usr/share/applications",
    "/usr/share/pixmaps",
)

# Prefixes under which *image files only* may be removed (icons installed by vendors).
SYSTEM_ICON_TREES = ("/usr/share/icons/hicolor", "/usr/local/share/icons")
ICON_PARENTS = ("/usr/share/pixmaps",)
ICON_EXTENSIONS = (".png", ".svg", ".svgz", ".xpm")

# Parents where only files/links (never directories) may be removed.
FILES_ONLY_PARENTS = (
    "/usr/local/bin", "/usr/local/sbin", "/usr/local/games",
    "/usr/local/share/applications", "/usr/share/applications", "/usr/share/pixmaps",
)

# Shared directories directly below /usr/local/lib and /usr/local/share.
USR_LOCAL_PROTECTED_CHILDREN = {
    "applications", "ca-certificates", "doc", "fonts", "icons", "info", "locale", "man",
    "mime", "pixmaps", "sgml", "xml", "emacs", "zsh", "fish", "vim", "nvim", "bash-completion",
    "node_modules", "pkgconfig", "site_ruby", "lua", "perl", "share", "systemd", "udev", "dbus-1",
    "polkit-1", "glib-2.0", "gir-1.0", "girepository-1.0", "gtk-2.0", "gtk-3.0", "gtk-4.0",
    "gnome-shell", "gstreamer-1.0", "modules", "firmware", "metainfo", "appdata", "themes",
    "sounds", "wayland-sessions", "xsessions", "games", "lib", "libexec", "include", "etc", "bin", "sbin",
}
_USR_LOCAL_PROTECTED_RE = re.compile(r"(-linux-gnu[a-z]*|^python[\d.]*|^perl\d*|^ruby|^node|^go$|^gcc|^llvm)", re.I)

HOME_SENSITIVE = (
    ".ssh", ".gnupg", ".password-store", ".pki", ".local/share/keyrings",
    ".mozilla", ".thunderbird", ".config/google-chrome", ".config/BraveSoftware",
)

HOME_PROTECTED = (
    "", ".local", ".local/bin", ".local/share", ".local/share/applications",
    ".local/share/icons", ".local/lib", ".local/state", ".config", ".cache", ".var",
    ".var/app", "Applications", "AppImages", "Apps", "bin", "Desktop", "Documents",
    "Downloads", "Music", "Pictures", "Public", "Templates", "Videos", "Projects",
    "snap", ".local/share/flatpak", ".local/share/Trash", "go", "go/bin", ".cargo", ".cargo/bin",
    ".rustup", ".npm", ".npm-global", ".npm-global/bin", ".deno", ".deno/bin", ".bun", ".bun/bin",
    ".pyenv", ".pyenv/shims", ".nvm", ".sdkman", ".local/pipx", ".volta", ".volta/bin",
)


def is_icon_file(path: str) -> bool:
    """A regular image file (never a directory or other file type)."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and path.lower().endswith(ICON_EXTENSIONS)


def _mount_points() -> list[str]:
    points = []
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as fh:
            for line in fh:
                fields = line.split()
                if len(fields) > 4:
                    # Mount points escape spaces etc. as octal (\040).
                    points.append(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), fields[4]))
    except OSError:
        pass
    return points


def mount_inside(path: str) -> str | None:
    """A mount point at or below ``path`` (bind mounts included), if any."""
    prefix = path.rstrip("/") + "/"
    for point in _mount_points():
        if point == path or point.startswith(prefix):
            return point
    return None


def home_dir() -> str:
    return os.path.realpath(os.path.expanduser("~"))


def _is_within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def protected_paths(home: str | None = None) -> set[str]:
    home = home or home_dir()
    paths = {"/", "/home", "/media", "/mnt", "/opt", "/tmp", "/usr", "/usr/local", "/usr/share",
             "/var", "/var/lib", "/var/tmp", "/usr/share/icons", "/usr/share/icons/hicolor",
             "/usr/local/share/icons"}
    paths.update(SYSTEM_REMOVABLE_PARENTS)
    paths.update(FORBIDDEN_TREES)
    for rel in HOME_PROTECTED:
        paths.add(os.path.normpath(os.path.join(home, rel)))
    return paths


def has_symlinked_parent(path: str) -> bool:
    """True if any directory component above ``path`` is a symlink."""
    parent = os.path.dirname(path)
    return os.path.realpath(parent) != parent


def removal_problem(path: str, home: str | None = None, owner_lookup=None) -> str | None:
    """Return a human readable reason why ``path`` must not be removed, or None.

    ``owner_lookup(path) -> str | None`` returns the owning package of a path
    (or of anything below a directory), when dpkg data is available.
    """
    try:
        validate_abs_path(path)
    except ValidationError as exc:
        return str(exc)
    home = home or home_dir()

    if path in protected_paths(home):
        return f"{path} is a protected system or user directory"
    for tree in FORBIDDEN_TREES:
        if _is_within(path, tree):
            return f"{path} is inside the protected system tree {tree}"
    if has_symlinked_parent(path):
        return f"{path} is reached through a symbolic link"

    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return f"{path} no longer exists"
    except OSError as exc:
        return f"{path} cannot be inspected: {exc.strerror}"

    in_home = _is_within(path, home)
    if in_home:
        rel = os.path.relpath(path, home)
        for sensitive in HOME_SENSITIVE:
            if _is_within(rel, sensitive) or _is_within(sensitive, rel):
                return f"{path} contains sensitive user data and is never removed automatically"
    else:
        parent = os.path.dirname(path)
        is_file_or_link = stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)
        in_icon_tree = any(_is_within(path, t) and path != t for t in SYSTEM_ICON_TREES) or parent in ICON_PARENTS
        if in_icon_tree:
            if not is_icon_file(path):
                return f"{path} is not an icon image; only icon files are removed there"
        elif parent not in SYSTEM_REMOVABLE_PARENTS:
            return f"{path} is outside the locations this app is allowed to clean up"
        base = os.path.basename(path)
        if parent in ("/usr/local/lib", "/usr/local/share") and (
                base in USR_LOCAL_PROTECTED_CHILDREN or _USR_LOCAL_PROTECTED_RE.search(base)):
            return f"{path} is a shared system directory"
        if is_file_or_link and SHARED_LIBRARY_RE.search(os.path.basename(path)):
            return f"{path} is a shared library and is never removed individually"
        if parent in FILES_ONLY_PARENTS and not is_file_or_link:
            return f"{path} is a directory in {parent}; only individual files are removed there"
        if parent in ("/usr/share/applications", "/usr/local/share/applications") and not path.endswith(".desktop"):
            return f"{path} is not a desktop launcher"

    if stat.S_ISDIR(st.st_mode):
        try:
            parent_dev = os.lstat(os.path.dirname(path)).st_dev
        except OSError:
            parent_dev = st.st_dev
        if parent_dev != st.st_dev:
            return f"{path} is a mount point"
        mount = mount_inside(path)
        if mount:
            return f"{path} contains a mount point ({mount})"
        if in_home and os.path.isdir(os.path.join(path, ".git")):
            return f"{path} is a Git working copy; remove it yourself if you really want to"
    elif not (stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)):
        return f"{path} is not a regular file, link or directory"

    if owner_lookup is not None:
        owner = owner_lookup(path)
        if owner:
            return f"{path} belongs to the package '{owner}' and must be removed with APT"
    return None


def is_safe_to_remove(path: str, home: str | None = None, owner_lookup=None) -> bool:
    return removal_problem(path, home, owner_lookup) is None
