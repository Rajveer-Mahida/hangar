"""Locating the privileged helper and building pkexec argument vectors.

Administrative operations always go through ``pkexec`` (PolicyKit) and the
helper in ``hangar/helper/hangar_helper.py``; the application itself never runs as
root and never builds shell strings.
"""

from __future__ import annotations

import grp
import os
import pwd
import shutil
import stat

from ..helper import hangar_helper

INSTALLED_HELPER_PATHS = (
    "/usr/libexec/hangar/hangar-helper",
    "/usr/local/libexec/hangar/hangar-helper",
)
DEV_HELPER_PATH = os.path.abspath(hangar_helper.__file__)
POLKIT_ACTION_PREFIX = "io.github.Rajveer_Mahida.Hangar."
# Development mode (running the in-tree helper through pkexec) must be requested
# explicitly; ./run.sh sets this. Installed copies never fall back to it.
DEV_MODE_ENV = "HANGAR_DEV_HELPER"

# pkexec exit statuses
PKEXEC_DISMISSED = 126
PKEXEC_NOT_AUTHORIZED = 127


class PrivilegeError(RuntimeError):
    pass


def _root_owned_chain(path: str) -> bool:
    """The helper and every parent directory must be owned by root and not writable by others."""
    current = path
    while True:
        try:
            st = os.lstat(current)
        except OSError:
            return False
        if st.st_uid != 0 or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return False
        if current == "/":
            return True
        current = os.path.dirname(current)


def installed_helper() -> str | None:
    for path in INSTALLED_HELPER_PATHS:
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode) and st.st_mode & stat.S_IXUSR and _root_owned_chain(path):
            return path
    return None


def pkexec_available() -> bool:
    return shutil.which("pkexec") is not None


def dev_mode_enabled() -> bool:
    return os.environ.get(DEV_MODE_ENV) == "1"


def _dev_helper_ok() -> str | None:
    """The in-tree helper must be owned by the user (or root) and not writable by others."""
    try:
        st = os.lstat(DEV_HELPER_PATH)
    except OSError as exc:
        return f"development helper missing: {exc.strerror}"
    if not stat.S_ISREG(st.st_mode):
        return "development helper is not a regular file"
    if st.st_uid not in (0, os.getuid()) or st.st_mode & stat.S_IWOTH:
        return "development helper is writable by other users"
    if st.st_mode & stat.S_IWGRP and not _private_group(st.st_gid):
        return "development helper is writable by a shared group"
    return None


def _private_group(gid: int) -> bool:
    """Ubuntu's default umask makes files group-writable for the user's private group."""
    try:
        group = grp.getgrgid(gid)
        user = pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return False
    return gid == os.getgid() and group.gr_name == user and set(group.gr_mem) <= {user}


def helper_status() -> dict[str, str | bool]:
    path = installed_helper()
    mode = "installed" if path else ("development" if dev_mode_enabled() else "unavailable")
    return {
        "pkexec": pkexec_available(),
        "installed": path is not None,
        "path": path or DEV_HELPER_PATH,
        "mode": mode,
    }


def validate_request(verb: str, args: list[str]) -> None:
    """Run the helper's own validation before asking for a password."""
    try:
        hangar_helper.build(verb, list(args))
    except hangar_helper.HelperError as exc:
        raise PrivilegeError(str(exc)) from exc
    except OSError as exc:
        raise PrivilegeError(exc.strerror or str(exc)) from exc


def helper_argv(verb: str, args: list[str]) -> list[str]:
    """argv that runs ``verb`` as root through pkexec."""
    if not pkexec_available():
        raise PrivilegeError("pkexec (PolicyKit) is not installed; administrative actions are unavailable.")
    validate_request(verb, args)
    pkexec = shutil.which("pkexec") or "/usr/bin/pkexec"
    helper = installed_helper()
    if helper:
        return [pkexec, helper, verb, *args]
    if not dev_mode_enabled():
        raise PrivilegeError("The privileged helper is not installed. Install the app with "
                             "'sudo ./scripts/install.sh' (or the .deb), or start it from the source tree "
                             "with ./run.sh to use development mode.")
    problem = _dev_helper_ok()
    if problem:
        raise PrivilegeError(f"Refusing to use the development helper: {problem}.")
    # Development mode: run the in-tree helper with an isolated interpreter.
    # PolicyKit asks for administrator authentication every time.
    return [pkexec, "/usr/bin/python3", "-I", "-B", DEV_HELPER_PATH, verb, *args]
