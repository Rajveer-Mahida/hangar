"""Release metadata stays in step with the version (CI builds releases from it)."""

import importlib.util
import os

import pytest

from hangar import VERSION

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("release_notes", os.path.join(ROOT, "scripts", "release-notes.py"))
release_notes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release_notes)


def test_current_version_has_release_notes():
    notes = release_notes.release_notes(VERSION)
    assert notes.strip()
    assert "<" not in notes                       # Markdown, not leftover XML


def test_lists_become_markdown_bullets(tmp_path):
    path = tmp_path / "metainfo.xml"
    path.write_text("""<component><releases>
      <release version="9.9.9"><description>
        <p>Intro   text.</p><ul><li>One <code>thing</code></li><li>Two</li></ul>
      </description></release>
    </releases></component>""")
    assert release_notes.release_notes("9.9.9", str(path)) == "Intro text.\n\n- One thing\n- Two\n"


def test_command_line_reads_metainfo_from_given_file(tmp_path):
    # CI passes the metainfo shipped in the .deb, so an older tag's notes can be read with
    # today's script.
    import subprocess
    import sys

    path = tmp_path / "metainfo.xml"
    path.write_text('<component><releases><release version="0.9"><description><p>Old.</p>'
                    "</description></release></releases></component>")
    script = os.path.join(ROOT, "scripts", "release-notes.py")
    out = subprocess.run([sys.executable, script, "0.9", "--metainfo", str(path)],
                         capture_output=True, text=True, check=True).stdout
    assert out == "Old.\n"


def test_unknown_version_is_an_error():
    with pytest.raises(LookupError):
        release_notes.release_notes("0.0.0-missing")
