import os
import struct
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def make_appimage(path, update_info: str | None = None, kind: int = 2, size: int = 4096) -> str:
    """Write a fake AppImage: an ELF header with the AppImage magic (never executed)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if update_info is None:
        data = bytearray(b"\x7fELF\x02\x01\x01\x00" + b"AI" + bytes([kind]) + b"\x00" * 5)
        data += b"\x00" * (size - len(data))
    else:
        data = build_elf_with_section(".upd_info", update_info.encode() + b"\x00", kind)
    with open(path, "wb") as fh:
        fh.write(bytes(data))
    os.chmod(path, 0o755)
    return path


def build_elf_with_section(name: str, payload: bytes, kind: int = 2) -> bytearray:
    """Minimal 64-bit little-endian ELF with a named section (enough for header parsing)."""
    shstrtab = b"\x00.shstrtab\x00" + name.encode() + b"\x00"
    header_size = 64
    shstr_off = header_size
    payload_off = shstr_off + len(shstrtab)
    shoff = payload_off + len(payload)
    shoff += (-shoff) % 8
    ident = b"\x7fELF\x02\x01\x01\x00" + b"AI" + bytes([kind]) + b"\x00" * 5
    header = bytearray(ident)
    header += struct.pack("<HHIQQQIHHHHHH", 2, 62, 1, 0, 0, shoff, 0, 64, 0, 0, 64, 3, 1)
    assert len(header) == 64
    body = bytearray(header) + shstrtab + payload
    body += b"\x00" * (shoff - len(body))

    def sh(name_off, sh_type, offset, size):
        return struct.pack("<IIQQQQIIQQ", name_off, sh_type, 0, 0, offset, size, 0, 0, 1, 0)

    body += sh(0, 0, 0, 0)
    body += sh(1, 3, shstr_off, len(shstrtab))
    body += sh(len(b"\x00.shstrtab\x00"), 7, payload_off, len(payload))
    return body


def write_desktop(path, name, exec_line, extra: str = "") -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"[Desktop Entry]\nType=Application\nName={name}\nExec={exec_line}\n{extra}")
    return path


def write_exec(path, content: bytes = b"\x7fELF" + b"\x00" * 60) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(content)
    os.chmod(path, 0o755)
    return path


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home
