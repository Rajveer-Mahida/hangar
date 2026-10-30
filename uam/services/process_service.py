"""Asynchronous subprocess execution integrated with the GLib main loop.

Processes are always started from argument vectors (Gio.Subprocess never
uses a shell). Output is streamed line by line to callbacks on the main
thread. Output is kept in memory only and is never written to the history.
"""

from __future__ import annotations

import collections
import re
from typing import Callable

from gi.repository import Gio, GLib

from .privilege_service import PKEXEC_DISMISSED, PKEXEC_NOT_AUTHORIZED

_APT_STATUS = re.compile(r"^(pmstatus|dlstatus):[^:]*:([\d.]+):(.*)$")
_PERCENT = re.compile(r"(\d{1,3}(?:\.\d+)?)\s?%")


class ProcessRunner:
    """Run one command; deliver lines and completion on the main loop."""

    def __init__(self, argv: list[str], on_line: Callable[[str], None] | None = None,
                 on_finished: Callable[[int, list[str]], None] | None = None,
                 env: dict[str, str] | None = None, keep_lines: int = 200):
        if not argv or not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
            raise ValueError("argv must be a non-empty list of strings")
        self.argv = argv
        self.on_line = on_line
        self.on_finished = on_finished
        self.env = env
        self.tail: collections.deque[str] = collections.deque(maxlen=keep_lines)
        self._proc: Gio.Subprocess | None = None
        self._stream: Gio.InputStream | None = None
        self._cancellable = Gio.Cancellable()
        self._buffer = b""
        self._eof = False
        self._exit: int | None = None

    def start(self) -> None:
        launcher = Gio.SubprocessLauncher.new(
            Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE | Gio.SubprocessFlags.STDIN_PIPE)
        if self.env is not None:
            launcher.set_environ([f"{k}={v}" for k, v in self.env.items()])
        self._proc = launcher.spawnv(self.argv)
        stdin = self._proc.get_stdin_pipe()
        if stdin is not None:
            stdin.close(None)
        self._stream = self._proc.get_stdout_pipe()
        self._read_next()
        self._proc.wait_async(self._cancellable, self._on_exit)

    def _read_next(self) -> None:
        self._stream.read_bytes_async(8192, GLib.PRIORITY_DEFAULT, self._cancellable, self._on_bytes)

    def _on_bytes(self, stream, result) -> None:
        try:
            chunk = stream.read_bytes_finish(result).get_data() or b""
        except GLib.Error:
            chunk = b""
        if not chunk:  # zero bytes: end of stream
            if self._buffer:
                self._emit_line(self._buffer)
                self._buffer = b""
            self._eof = True
            self._maybe_finish()
            return
        self._buffer += chunk
        *lines, self._buffer = self._buffer.split(b"\n")
        for raw in lines:
            self._emit_line(raw)
        if len(self._buffer) > 65536:  # pathological output without newlines
            self._emit_line(self._buffer)
            self._buffer = b""
        self._read_next()

    def _emit_line(self, raw: bytes) -> None:
        text = raw.decode("utf-8", "replace")
        # Progress bars redraw with carriage returns; keep the latest state.
        text = text.rstrip("\r").split("\r")[-1].rstrip()
        if text:
            self.tail.append(text)
            if self.on_line:
                self.on_line(text)

    def _on_exit(self, proc, result) -> None:
        try:
            proc.wait_finish(result)
        except GLib.Error:
            pass
        if proc.get_if_exited():
            self._exit = proc.get_exit_status()
        else:
            self._exit = 128 + (proc.get_term_sig() if proc.get_if_signaled() else 0)
        self._maybe_finish()

    def _maybe_finish(self) -> None:
        if self._eof and self._exit is not None and self.on_finished:
            callback, self.on_finished = self.on_finished, None
            callback(self._exit, list(self.tail))

    def cancel(self) -> None:
        """Only meaningful for unprivileged processes; root processes cannot be signalled."""
        if self._proc is not None:
            self._proc.force_exit()


def parse_progress(line: str) -> tuple[float | None, str | None]:
    """Extract (fraction, status message) from package manager output."""
    m = _APT_STATUS.match(line)
    if m:
        try:
            pct = float(m.group(2))
        except ValueError:
            pct = None
        kind = m.group(1)
        # apt reports download and install phases separately: map to 0-30% / 30-100%.
        if pct is not None:
            fraction = pct / 100 * (0.3 if kind == "dlstatus" else 0.7) + (0 if kind == "dlstatus" else 0.3)
        else:
            fraction = None
        return fraction, m.group(3).strip()
    m = _PERCENT.search(line)
    if m:
        try:
            return min(float(m.group(1)), 100.0) / 100.0, None
        except ValueError:
            pass
    return None, None


def is_status_line(line: str) -> bool:
    return bool(_APT_STATUS.match(line))


_DPKG_ERROR = re.compile(r"^dpkg: error processing (?:package|archive) \S+ \(--\w+\):$")
_DPKG_RETRY = ("Other updates may have been applied. Run 'sudo dpkg --configure -a' in a terminal "
               "to retry and see the full error.")


def _explain_dpkg_failure(tail: list[str], include_output: bool) -> str:
    if not include_output:
        return f"dpkg could not finish installing some packages. {_DPKG_RETRY}"
    lines = [line.rstrip() for line in tail]
    names: list[str] = []
    reason = ""
    for i, line in enumerate(lines):
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if not reason and _DPKG_ERROR.match(line) and nxt.startswith(" "):
            reason = nxt.strip()
        if line == "Errors were encountered while processing:":
            names = []
            for item in lines[i + 1:]:
                if not item.startswith(" "):
                    break
                names.append(item.strip())
    shown = ", ".join(names[:5]) + (f" and {len(names) - 5} more" if len(names) > 5 else "")
    message = f"dpkg could not finish installing: {shown}." if shown else "dpkg could not finish installing."
    if reason:
        message += f"\n\nFirst error: {reason}."
    return f"{message}\n\n{_DPKG_RETRY}"


_SNAP_BUSY = re.compile(r'snap "([^"]+)" has running apps')


def _explain_busy_snaps(tail: list[str], include_output: bool) -> str:
    # snapd refreshes each snap of a batch in its own transaction, so only the open ones failed.
    others = "Other snaps in the same update are refreshed separately and were not held back."
    names = list(dict.fromkeys(m.group(1) for line in tail for m in _SNAP_BUSY.finditer(line)))
    if not include_output or not names:
        return f"A snap could not be updated because it was running. Close it and try again.\n\n{others}"
    if len(names) == 1:
        return (f"{names[0]} is running, and snaps can't be updated while they're open. "
                f"Close {names[0]} and update it again.\n\n{others}")
    return (f"These snaps are running and can't be updated while they're open: {', '.join(names)}. "
            f"Close them and update them again.\n\n{others}")


_ERROR_PATTERNS = [
    (re.compile(r"Could not get lock|Unable to acquire the dpkg frontend lock|is another process using it", re.I),
     "Another package manager is running (for example Software Updater or unattended upgrades). "
     "Wait for it to finish and try again."),
    (re.compile(r"dpkg was interrupted", re.I),
     "A previous package operation was interrupted. Open a terminal and run "
     "'sudo dpkg --configure -a', then try again."),
    (re.compile(r"Unable to locate package|has no installation candidate", re.I),
     "The package is not available from the configured repositories."),
    (re.compile(r"Temporary failure resolving|Could not resolve|Network is unreachable|"
                r"Failed to fetch|unable to contact snap store|Could not connect", re.I),
     "A network error occurred. Check your internet connection and try again."),
    (re.compile(r"No space left on device|not enough free space", re.I),
     "There is not enough free disk space."),
    # A package script or unpack failed. apt's last line ("Sub-process /usr/bin/dpkg returned an
    # error code (1)") says nothing useful, so the message is built from dpkg's own lines.
    (re.compile(r"^Errors were encountered while processing:", re.M), _explain_dpkg_failure),
    (_SNAP_BUSY, _explain_busy_snaps),
    (re.compile(r"has running apps|is running", re.I),
     "The application is currently running. Close it and try again."),
    (re.compile(r"packages that would be removed changed since the preview", re.I),
     "The system changed since the preview was shown. Review the removal again."),
    (re.compile(r"held broken packages|unmet dependencies", re.I),
     "APT cannot resolve the package dependencies. Run 'sudo apt --fix-broken install' in a terminal."),
    (re.compile(r"Essential|is protected|cannot remove .* required", re.I),
     "The package is essential for the system and cannot be removed."),
    (re.compile(r"is needed by|needed by the following installed refs|used by", re.I),
     "Another installed component still depends on this item."),
    (re.compile(r"not installed", re.I),
     "The item is not installed (it may already have been removed)."),
    (re.compile(r"REFUSED: ", re.I),
     "The safety checks refused to remove one or more paths. Nothing was changed."),
]


def explain_failure(exit_code: int, tail: list[str], privileged: bool, include_output: bool = True) -> str:
    """Translate an exit status and output tail into a user-facing message.

    With ``include_output=False`` only fixed, known messages are returned
    (never text copied from the command output); used for the history.
    """
    if privileged and exit_code == PKEXEC_DISMISSED:
        return "Authentication was cancelled. No changes were made."
    if privileged and exit_code == PKEXEC_NOT_AUTHORIZED:
        return "You are not authorized to perform this action, or authentication failed."
    text = "\n".join(tail[-60:])
    for pattern, message in _ERROR_PATTERNS:
        if pattern.search(text):
            return message(tail[-60:], include_output) if callable(message) else message
    if not include_output:
        return f"The operation failed (exit status {exit_code})."
    for line in reversed(tail):
        stripped = line.strip()
        if stripped.startswith(("E:", "error:", "ERROR:", "Error:")):
            return stripped.split(":", 1)[1].strip() or stripped
    return f"The operation failed (exit status {exit_code})."
