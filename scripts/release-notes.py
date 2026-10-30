#!/usr/bin/env python3
"""Print the release notes of one version as Markdown, taken from the AppStream metainfo.

The metainfo <release> entry is the single place release notes are written; CI uses this
script for the GitHub release text.  Usage: scripts/release-notes.py [VERSION]
"""

import os
import sys
import xml.etree.ElementTree as ET

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SRC)

from uam import APP_ID, VERSION  # noqa: E402

METAINFO = os.path.join(SRC, "data", f"{APP_ID}.metainfo.xml")


def _text(element: ET.Element) -> str:
    return " ".join("".join(element.itertext()).split())


def release_notes(version: str, path: str = METAINFO) -> str:
    """Markdown for ``version``; raises LookupError if the metainfo has no such release."""
    for release in ET.parse(path).getroot().iterfind("releases/release"):
        if release.get("version") != version:
            continue
        lines: list[str] = []
        description = release.find("description")
        for block in (description if description is not None else []):
            if block.tag == "p":
                lines += [_text(block), ""]
            elif block.tag in ("ul", "ol"):
                lines += [f"- {_text(item)}" for item in block.iterfind("li")] + [""]
        return "\n".join(lines).strip() + "\n"
    raise LookupError(f"{os.path.basename(path)} has no <release version=\"{version}\"> entry")


if __name__ == "__main__":
    try:
        sys.stdout.write(release_notes(sys.argv[1] if len(sys.argv) > 1 else VERSION))
    except LookupError as exc:
        sys.exit(f"error: {exc}")
