"""Operation queue: real (harmless) subprocesses, progress, failures and history hygiene."""

import shutil

import pytest
from gi.repository import GLib

from hangar.core.database import Database
from hangar.services.operation_service import CommandStep, Operation, OperationManager, PythonStep


def run_until_idle(manager: OperationManager, timeout: float = 20.0) -> None:
    loop = GLib.MainLoop()

    def check():
        if not manager.busy:
            loop.quit()
            return False
        return True

    GLib.timeout_add(50, check)
    GLib.timeout_add(int(timeout * 1000), loop.quit)
    loop.run()


@pytest.mark.skipif(not shutil.which("printf"), reason="printf not available")
def test_successful_command_and_history_without_output():
    db = Database(":memory:")
    manager = OperationManager(db)
    secret = "SECRET-OUTPUT-LINE"
    printf = shutil.which("printf")
    op = manager.submit(Operation("Demo", "update", ["Demo App"],
                                  [CommandStep([printf, f"pmstatus:x:50:half\\n{secret}\\n"], "Running demo")]))
    run_until_idle(manager)
    assert op.status == "success" and op.fraction == 1.0
    assert any(secret in line for line in op.log)          # visible live in the UI…
    history = db.history()
    assert history[0]["status"] == "success" and history[0]["target"] == "Demo App"
    assert all(secret not in (h["detail"] or "") for h in history)  # …but never stored


def test_failing_steps_stop_the_operation_and_are_explained():
    db = Database(":memory:")
    manager = OperationManager(db)
    false = shutil.which("false") or "/bin/false"
    ran = []
    op = manager.submit(Operation("Broken", "remove", ["Thing"], [
        CommandStep([false], "Failing step"),
        PythonStep(lambda log: ran.append(True), "Never runs"),
    ]))
    run_until_idle(manager)
    assert op.status == "failed" and "exit status 1" in op.error
    assert ran == []
    assert db.history()[0]["status"] == "failed"


def test_python_step_errors_are_reported():
    db = Database(":memory:")
    manager = OperationManager(db)

    def boom(log):
        log("starting")
        raise RuntimeError("could not move file to Trash")

    op = manager.submit(Operation("Trash", "remove", ["X"], [PythonStep(boom, "Trashing")]))
    run_until_idle(manager)
    assert op.status == "failed" and "Trash" in op.error


def test_missing_program_fails_cleanly():
    db = Database(":memory:")
    manager = OperationManager(db)
    op = manager.submit(Operation("Missing", "install", ["Y"], [CommandStep(["/nonexistent/binary"], "x")]))
    run_until_idle(manager)
    assert op.status == "failed" and "Could not start" in op.error


def test_operations_run_one_at_a_time_in_order():
    db = Database(":memory:")
    manager = OperationManager(db)
    order = []
    ops = [manager.submit(Operation(f"Op {i}", "update", [str(i)],
                                    [PythonStep(lambda log, i=i: order.append(i), "step")])) for i in range(3)]
    queued = manager.submit(Operation("Cancelled", "update", ["c"], [PythonStep(lambda log: order.append("c"), "s")]))
    assert manager.cancel(queued)
    run_until_idle(manager)
    assert order == [0, 1, 2]
    assert [o.status for o in ops] == ["success"] * 3 and queued.status == "cancelled"


def test_process_runner_splits_lines_and_carriage_returns():
    from hangar.services.process_service import ProcessRunner

    printf = shutil.which("printf") or "/usr/bin/printf"
    lines, result = [], {}
    loop = GLib.MainLoop()

    def done(code, tail):
        result["code"] = code
        loop.quit()

    ProcessRunner([printf, "crlf\\r\\n10%%\\r55%%\\nno-newline"], on_line=lines.append, on_finished=done).start()
    GLib.timeout_add(10000, loop.quit)
    loop.run()
    assert result["code"] == 0
    assert lines == ["crlf", "55%", "no-newline"]
