"""Snap integration through the snapd REST API (read-only, unprivileged)."""

from __future__ import annotations

import http.client
import json
import logging
import os
import re
import socket
import urllib.parse
from datetime import datetime

from ..core.application_model import AppRecord, Confidence, PackageManager, Source, UpdateInfo
from ..core.desktop_entry import parse_desktop_file
from ..core.safety import SNAP_NAME_RE
from .base import Provider, ScanContext

log = logging.getLogger(__name__)

SNAPD_SOCKET = "/run/snapd.socket"

# Snaps that only exist to support other snaps.
_COMPONENT_RE = re.compile(
    r"^(core\d*|bare|snapd.*|gtk-common-themes|.*-themes|gnome-\d.*|kde-.*|mesa-.*|qt\d.*|"
    r"wine-platform.*|.*-runtime.*|.*-platform.*|cups|firmware-updater|desktop-security-center|"
    r"prompting-client|bluez|network-manager|pulseaudio)$"
)


class SnapdError(RuntimeError):
    pass


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.socket_path)
        self.sock = sock


class SnapdClient:
    def __init__(self, socket_path: str = SNAPD_SOCKET, timeout: float = 15.0):
        self.socket_path = socket_path
        self.timeout = timeout

    def _request(self, path: str) -> tuple[int, bytes, str]:
        conn = _UnixHTTPConnection(self.socket_path, self.timeout)
        try:
            conn.request("GET", path, headers={"Accept": "application/json"})
            resp = conn.getresponse()
            return resp.status, resp.read(), resp.getheader("Content-Type", "")
        except (OSError, http.client.HTTPException) as exc:
            raise SnapdError(f"snapd is not reachable: {exc}") from exc
        finally:
            conn.close()

    def get(self, path: str):
        status, body, _ = self._request(path)
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise SnapdError("Invalid response from snapd") from exc
        if status >= 400 or data.get("type") == "error":
            result = data.get("result") or {}
            message = result.get("message") if isinstance(result, dict) else None
            raise SnapdError(message or f"snapd returned HTTP {status}")
        return data.get("result")

    def get_icon(self, name: str) -> bytes | None:
        if not SNAP_NAME_RE.match(name):
            return None
        status, body, ctype = self._request(f"/v2/icons/{urllib.parse.quote(name)}/icon")
        if status == 200 and ctype.startswith("image/"):
            return body
        return None

    def version(self) -> str:
        try:
            result = self.get("/v2/system-info")
            return f"snapd {result.get('version', '')}"
        except SnapdError:
            return ""


def _parse_date(text: str) -> float | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _icon_cache_dir() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "hangar", "icons", "snap")


class SnapProvider(Provider):
    id = "snap"
    label = "Snap"
    icon_name = "application-x-addon-symbolic"
    description = "Snap packages managed by snapd"

    def __init__(self, client: SnapdClient | None = None):
        self.client = client or SnapdClient()

    def available(self) -> bool:
        return os.path.exists(self.client.socket_path)

    def tool_version(self) -> str:
        return self.client.version()

    def supports_updates(self) -> bool:
        return True

    def _cached_icon(self, name: str) -> str:
        directory = _icon_cache_dir()
        path = os.path.join(directory, f"{name}.png")
        if os.path.exists(path):
            return path
        try:
            data = self.client.get_icon(name)
        except SnapdError:
            data = None
        if not data:
            return ""
        try:
            os.makedirs(directory, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
            return path
        except OSError:
            return ""

    def scan(self, ctx: ScanContext) -> list[AppRecord]:
        try:
            snaps = self.client.get("/v2/snaps") or []
        except SnapdError as exc:
            ctx.errors.append(f"Snap: {exc}")
            return []
        records = []
        for snap in snaps:
            name = snap.get("name", "")
            if not SNAP_NAME_RE.match(name):
                continue
            apps = [a for a in snap.get("apps", []) if not a.get("daemon")]
            desktop_files = [a["desktop-file"] for a in apps if a.get("desktop-file")]
            entries = [e for e in (parse_desktop_file(p) for p in desktop_files) if e]
            visible = [e for e in entries if e.visible]
            primary = visible[0] if visible else (entries[0] if entries else None)
            snap_type = snap.get("type", "app")
            is_component = snap_type != "app" or bool(_COMPONENT_RE.match(name)) or (
                not apps and not desktop_files)
            has_launcher = primary is not None

            publisher = snap.get("publisher") or {}
            origin = publisher.get("display-name") or publisher.get("username") or ""
            if publisher.get("validation") in ("verified", "starred"):
                origin += " ✓"
            notes = []
            confinement = snap.get("confinement", "")
            if confinement == "classic":
                notes.append("Classic confinement: this snap is not sandboxed and has full system access.")
            elif confinement == "devmode":
                notes.append("Development mode: confinement is not enforced.")
            if snap.get("status") not in (None, "active"):
                notes.append(f"Snap status: {snap.get('status')}")

            current = f"/snap/{name}/current"
            install_path = os.path.realpath(current) if os.path.exists(current) else f"/snap/{name}"
            exec_path = ""
            if apps:
                app_name = apps[0].get("name", name)
                exec_path = f"/snap/bin/{name}" if app_name == name else f"/snap/bin/{name}.{app_name}"
            icon = primary.icon if primary and primary.icon else ""
            if not icon and not is_component:
                icon = self._cached_icon(name)

            record = AppRecord(
                key=f"snap:{name}",
                name=(primary.name if primary and primary.name else snap.get("title") or name),
                source=Source.SNAP,
                package_manager=PackageManager.SNAP,
                confidence=Confidence.MANAGED,
                summary=snap.get("summary") or (primary.comment if primary else ""),
                description=snap.get("description", "").strip(),
                icon=icon,
                package_name=name,
                version=snap.get("version", ""),
                install_date=_parse_date(snap.get("install-date", "")),
                install_path=install_path,
                exec_path=exec_path,
                exec_command=primary.exec_line if primary else "",
                desktop_file=primary.path if primary else "",
                desktop_id=primary.desktop_id if primary else "",
                categories=primary.categories if primary else [],
                size=snap.get("installed-size"),
                origin=origin.strip(),
                homepage=snap.get("website", "") or "",
                is_component=is_component,
                hidden=bool(primary and not primary.visible),
                has_launcher=has_launcher,
                related_paths=[e.path for e in entries if e is not primary],
                notes=notes,
                extra={
                    "type": snap_type,
                    "channel": snap.get("tracking-channel") or snap.get("channel", ""),
                    "revision": snap.get("revision", ""),
                    "confinement": confinement,
                    "base": snap.get("base", ""),
                    "commands": [a.get("name") for a in apps],
                    "terminal": bool(primary and primary.terminal),
                },
            )
            records.append(record)
            for e in entries:
                ctx.claim(e.path)
                ctx.managed_desktop_ids.add(e.desktop_id)
                ctx.managed_desktop_ids.update(a.get("common-id", "") for a in apps if a.get("common-id"))
        return records

    def check_updates(self, records: list[AppRecord]) -> list[UpdateInfo]:
        result = self.client.get("/v2/find?select=refresh") or []
        by_name = {r.package_name: r for r in records if r.package_manager == PackageManager.SNAP}
        updates = []
        for item in result:
            name = item.get("name", "")
            record = by_name.get(name)
            updates.append(UpdateInfo(
                source="snap",
                package_name=name,
                name=record.name if record else item.get("title") or name,
                current_version=record.version if record else "",
                new_version=item.get("version", ""),
                download_size=item.get("download-size"),
                is_application=bool(record and not record.is_component),
                origin=(item.get("publisher") or {}).get("display-name", ""),
                app_keys=[record.key] if record else [],
            ))
        return updates
