"""What the controller does after an operation finishes."""

from types import SimpleNamespace

from hangar.services.inventory_service import AppController


def _finish(op):
    calls = []
    fake = SimpleNamespace(
        emit=lambda *args: calls.append(("emit", args[0])),
        check_updates=lambda sources, quiet=False: calls.append(("check", tuple(sources), quiet)),
        operations=SimpleNamespace(busy=True),
    )
    AppController._on_operation_finished(fake, None, op)
    return calls


def test_failed_update_rechecks_its_source():
    # A failed batch may still have updated some packages (snapd refreshes each snap on its
    # own; dpkg configures the packages it can), so the list must be checked again.
    op = SimpleNamespace(status="failed", kind="update", source="Snap", update_keys=["snap:brave"])
    assert ("check", ("snap",), True) in _finish(op)
    op.source = "APT"
    assert ("check", ("apt",), True) in _finish(op)


def test_failed_removal_does_not_check_updates():
    op = SimpleNamespace(status="failed", kind="remove", source="Snap", app_keys=["snap:brave"])
    assert not [c for c in _finish(op) if c[0] == "check"]
