"""Flatpak integration through the flatpak command line (argument arrays only)."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess

from ..core.application_model import (
    AppRecord,
    Confidence,
    PackageManager,
    Source,
    UpdateInfo,
    parse_size,
)
from ..core.desktop_entry import parse_desktop_file
from ..core.safety import FLATPAK_REF_RE
from .base import Provider, ScanContext

log = logging.getLogger(__name__)

LIST_COLUMNS = "application,name,description,version,branch,arch,origin,installation,size,ref,options"
UPDATE_COLUMNS = "application,version,branch,arch,origin,download-size,ref"


def installation_base(installation: str) -> str:
    if installation == "system":
        return "/var/lib/flatpak"
    if installation == "user":
        data = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
        return os.path.join(data, "flatpak")
    return ""


def _flatpak_env() -> dict[str, str]:
    env = dict(os.environ)
    env["LC_NUMERIC"] = "C"
    env.pop("LC_ALL", None)
    return env


def _resolve_icon(base: str, icon: str) -> str:
    """Return an absolute path for an exported icon, or the icon name."""
    if not icon or os.path.isabs(icon) or not base:
        return icon
    icon_root = os.path.join(base, "exports", "share", "icons", "hicolor")
    for size in ("scalable", "512x512", "256x256", "128x128", "96x96", "64x64", "48x48"):
        for ext in (".svg", ".png"):
            candidate = os.path.join(icon_root, size, "apps", icon + ext)
            if os.path.exists(candidate):
                return candidate
    return icon


def parse_list_output(text: str) -> list[dict[str, str]]:
    keys = LIST_COLUMNS.split(",")
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < len(keys):
            parts += [""] * (len(keys) - len(parts))
        rows.append(dict(zip(keys, parts)))
    return rows


def parse_update_output(text: str, installation: str) -> list[dict[str, str]]:
    keys = UPDATE_COLUMNS.split(",")
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < len(keys):
            parts += [""] * (len(keys) - len(parts))
        row = dict(zip(keys, parts))
        row["installation"] = installation
        rows.append(row)
    return rows


class FlatpakProvider(Provider):
    id = "flatpak"
    label = "Flatpak"
    icon_name = "system-software-install-symbolic"
    description = "Sandboxed Flatpak applications and runtimes"

    def __init__(self, binary: str | None = None):
        self.binary = binary or shutil.which("flatpak") or ""

    def available(self) -> bool:
        return bool(self.binary)

    def tool_version(self) -> str:
        try:
            out = subprocess.run([self.binary, "--version"], capture_output=True, text=True, timeout=5)
            return out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""

    def supports_updates(self) -> bool:
        return True

    def _run(self, args: list[str], timeout: float = 60) -> str:
        proc = subprocess.run([self.binary, *args], capture_output=True, text=True, timeout=timeout,
                              env=_flatpak_env())
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else
                               f"flatpak exited with status {proc.returncode}")
        return proc.stdout

    def scan(self, ctx: ScanContext) -> list[AppRecord]:
        try:
            output = self._run(["list", f"--columns={LIST_COLUMNS}"])
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            ctx.errors.append(f"Flatpak: {exc}")
            return []
        return self.records_from_rows(parse_list_output(output), ctx)

    def records_from_rows(self, rows: list[dict[str, str]], ctx: ScanContext | None) -> list[AppRecord]:
        records = []
        for row in rows:
            app_id = row["application"]
            ref = row["ref"]
            if not FLATPAK_REF_RE.match(ref or ""):
                continue
            options = row["options"].split(",")
            is_runtime = "runtime" in options
            installation = row["installation"] or ("user" if "user" in options else "system")
            base = installation_base(installation)
            arch, branch = row["arch"], row["branch"]
            deploy = os.path.join(base, "runtime" if is_runtime else "app", app_id, arch, branch, "active") if base else ""
            install_path = os.path.realpath(deploy) if deploy and os.path.exists(deploy) else ""
            install_date = None
            if base:
                try:
                    install_date = os.stat(os.path.join(base, "runtime" if is_runtime else "app", app_id)).st_mtime
                except OSError:
                    pass

            entry = None
            if not is_runtime and base:
                desktop_path = os.path.join(base, "exports", "share", "applications", f"{app_id}.desktop")
                entry = parse_desktop_file(desktop_path)
            notes = []
            eol = [o for o in options if o.startswith("eol=")]
            if eol:
                notes.append("End of life: " + eol[0][4:])

            name = (entry.name if entry and entry.name else row["name"]) or app_id
            if is_runtime and row["branch"]:
                name = f"{name} {row['branch']}"
            record = AppRecord(
                key=f"flatpak:{installation}:{ref}",
                name=name,
                source=Source.FLATPAK,
                package_manager=PackageManager.FLATPAK,
                confidence=Confidence.MANAGED,
                summary=row["description"] or (entry.comment if entry else ""),
                description=row["description"],
                icon=_resolve_icon(base, entry.icon if entry else "") if entry else "",
                package_name=app_id,
                version=row["version"] or branch,
                install_date=install_date,
                install_path=install_path,
                exec_path=self.binary,
                exec_command=entry.exec_line if entry else "",
                desktop_file=entry.path if entry else "",
                desktop_id=entry.desktop_id if entry else "",
                categories=entry.categories if entry else [],
                size=parse_size(row["size"]),
                origin=row["origin"],
                is_component=is_runtime,
                hidden=bool(entry and not entry.visible),
                has_launcher=entry is not None,
                notes=notes,
                extra={
                    "ref": ref,
                    "installation": installation,
                    "branch": branch,
                    "arch": arch,
                    "data_dir": os.path.join(os.path.expanduser("~"), ".var", "app", app_id),
                    "terminal": bool(entry and entry.terminal),
                },
            )
            records.append(record)
            if ctx is not None and entry is not None:
                ctx.claim(entry.path)
                ctx.managed_desktop_ids.add(entry.desktop_id)
        return records

    def check_updates(self, records: list[AppRecord]) -> list[UpdateInfo]:
        by_ref: dict[tuple[str, str], AppRecord] = {}
        for r in records:
            if r.package_manager == PackageManager.FLATPAK:
                by_ref[(r.extra.get("installation", ""), r.extra.get("ref", ""))] = r
        installations = sorted({inst for inst, _ in by_ref} & {"system", "user"}) or ["system"]
        updates = []
        errors = []
        for installation in installations:
            try:
                out = self._run(["remote-ls", "--updates", f"--{installation}", f"--columns={UPDATE_COLUMNS}"],
                                timeout=120)
            except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
                errors.append(str(exc))
                continue
            for row in parse_update_output(out, installation):
                ref = row["ref"]
                short_ref = ref.split("/", 1)[1] if ref.startswith(("app/", "runtime/")) else ref
                record = by_ref.get((installation, short_ref)) or by_ref.get((installation, ref))
                updates.append(UpdateInfo(
                    source="flatpak",
                    package_name=short_ref or row["application"],
                    name=record.name if record else row["application"],
                    current_version=record.version if record else "",
                    new_version=row["version"] or row["branch"],
                    download_size=parse_size(row["download-size"]),
                    installation=installation,
                    is_application=bool(record and not record.is_component),
                    origin=row["origin"],
                    app_keys=[record.key] if record else [],
                ))
        if errors and not updates:
            raise RuntimeError("; ".join(errors))
        return updates
