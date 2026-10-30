"""Queue of package operations with progress reporting and history logging.

Package managers hold exclusive locks, so operations run one at a time.
Every operation is recorded in the history (target, status, short message);
command output is only kept in memory for the progress view.
"""

from __future__ import annotations

import collections
import threading
import time
from dataclasses import dataclass
from typing import Callable

from gi.repository import GLib, GObject

from ..core.database import Database
from .process_service import ProcessRunner, explain_failure, is_status_line, parse_progress


@dataclass
class CommandStep:
    argv: list[str]
    description: str
    privileged: bool = False
    env: dict[str, str] | None = None


@dataclass
class PythonStep:
    """Work executed in a worker thread. ``func(log)`` raises on failure."""

    func: Callable[[Callable[[str], None]], None]
    description: str


Step = CommandStep | PythonStep


class Operation(GObject.Object):
    __gtype_name__ = "UamOperation"
    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self, title: str, kind: str, targets: list[str], steps: list[Step], source: str = "",
                 app_keys: list[str] | None = None, update_keys: list[str] | None = None):
        super().__init__()
        self.title = title
        self.kind = kind          # remove / update / install / refresh / cleanup
        self.targets = targets
        self.steps = steps
        self.source = source
        self.app_keys = app_keys or []
        self.update_keys = update_keys or []
        self.status = "queued"
        self.fraction: float | None = None
        self.message = "Waiting"
        self.error = ""
        self.history_detail = ""   # fixed message safe to store (no command output)
        self.log: collections.deque[str] = collections.deque(maxlen=400)
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.step_index = 0
        self._runner: ProcessRunner | None = None

    @property
    def finished(self) -> bool:
        return self.status in ("success", "failed", "cancelled")

    @property
    def privileged(self) -> bool:
        return any(isinstance(s, CommandStep) and s.privileged for s in self.steps)

    def _emit(self) -> None:
        self.emit("changed")


class OperationManager(GObject.Object):
    __gtype_name__ = "UamOperationManager"
    __gsignals__ = {
        "queue-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "operation-finished": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self.queue: list[Operation] = []
        self.current: Operation | None = None
        self.recent: collections.deque[Operation] = collections.deque(maxlen=20)

    @property
    def busy(self) -> bool:
        return self.current is not None or bool(self.queue)

    def submit(self, op: Operation) -> Operation:
        self.queue.append(op)
        self.emit("queue-changed")
        if self.current is None:
            GLib.idle_add(self._start_next)
        return op

    def cancel(self, op: Operation) -> bool:
        """Queued operations can always be cancelled; running ones only if unprivileged."""
        if op in self.queue:
            self.queue.remove(op)
            op.status = "cancelled"
            op.message = "Cancelled"
            self._record(op)
            op._emit()
            self.emit("queue-changed")
            self.emit("operation-finished", op)
            return True
        if op is self.current and not op.privileged and op._runner is not None:
            op.error = "Cancelled by user"
            op.status = "cancelled"
            op._runner.cancel()
            return True
        return False

    # -- execution ------------------------------------------------------------
    def _start_next(self) -> bool:
        if self.current is not None or not self.queue:
            return False
        op = self.queue.pop(0)
        self.current = op
        op.status = "running"
        op.started_at = time.time()
        op.step_index = 0
        self.emit("queue-changed")
        self._run_step(op)
        return False

    def _run_step(self, op: Operation) -> None:
        if op.status == "cancelled":
            self._finish(op)
            return
        if op.step_index >= len(op.steps):
            op.status = "success"
            op.fraction = 1.0
            op.message = "Completed"
            self._finish(op)
            return
        step = op.steps[op.step_index]
        op.message = step.description
        op.fraction = None
        op._emit()
        if isinstance(step, CommandStep):
            self._run_command(op, step)
        else:
            self._run_python(op, step)

    def _step_done(self, op: Operation, ok: bool, error: str = "") -> None:
        if not ok:
            if op.status != "cancelled":
                op.status = "failed"
            op.error = error or op.error or "The operation failed."
            op.message = op.error
            self._finish(op)
            return
        op.step_index += 1
        self._run_step(op)

    def _run_command(self, op: Operation, step: CommandStep) -> None:
        n = len(op.steps)

        def on_line(line: str) -> None:
            fraction, status = parse_progress(line)
            if fraction is not None:
                op.fraction = (op.step_index + fraction) / n
            if status:
                op.message = status
            if not is_status_line(line):
                op.log.append(line)
            op._emit()

        def on_finished(code: int, tail: list[str]) -> None:
            op._runner = None
            if code == 0:
                self._step_done(op, True)
            else:
                lines = [l for l in tail if not is_status_line(l)]
                op.history_detail = explain_failure(code, lines, step.privileged, include_output=False)
                self._step_done(op, False, explain_failure(code, lines, step.privileged))

        op.log.append(f"$ {' '.join(step.argv)}")
        try:
            runner = ProcessRunner(step.argv, on_line=on_line, on_finished=on_finished, env=step.env)
            op._runner = runner
            runner.start()
        except (GLib.Error, ValueError) as exc:
            op._runner = None
            message = exc.message if isinstance(exc, GLib.Error) else str(exc)
            self._step_done(op, False, f"Could not start {step.argv[0]}: {message}")

    def _run_python(self, op: Operation, step: PythonStep) -> None:
        def log(line: str) -> None:
            def add():
                op.log.append(line)
                op._emit()
                return False
            GLib.idle_add(add)

        def worker():
            try:
                step.func(log)
            except Exception as exc:  # reported to the user, not swallowed
                message = str(exc) or exc.__class__.__name__
                GLib.idle_add(lambda: self._step_done(op, False, message) or False)
                return
            GLib.idle_add(lambda: self._step_done(op, True) or False)

        threading.Thread(target=worker, daemon=True, name=f"op-{op.kind}").start()

    def _finish(self, op: Operation) -> None:
        op.finished_at = time.time()
        self._record(op)
        op._emit()
        self.current = None
        self.recent.appendleft(op)
        self.emit("operation-finished", op)
        self.emit("queue-changed")
        GLib.idle_add(self._start_next)

    def _record(self, op: Operation) -> None:
        detail = (op.history_detail or op.error) if op.status != "success" else op.title
        try:
            self.db.add_history(op.kind, ", ".join(op.targets) or op.title, op.status, op.source, detail)
        except Exception:  # history must never break an operation
            pass
