# Packaging as a .deb

## Today: binary package

`scripts/build-deb.sh` produces `dist/hangar_<version>_all.deb` (architecture `all`, pure
Python). It installs:

| Path | Content |
| --- | --- |
| `/usr/bin/hangar` | launcher script (`python3 -m uam`) |
| `/usr/share/hangar/uam/` | the Python package |
| `/usr/libexec/hangar/hangar-helper` | root helper, 0755 root:root |
| `/usr/share/polkit-1/actions/io.github.Rajveer_Mahida.Hangar.policy` | PolicyKit action bound to the helper path |
| `/usr/share/applications/…desktop`, `/usr/share/icons/hicolor/…`, `/usr/share/metainfo/…` | desktop integration |

Icon cache and desktop database are refreshed by the standard dpkg triggers of `hicolor-icon-theme`
and `desktop-file-utils`, so no maintainer scripts are needed.

## Next step: a Debian source package (for a PPA or the archive)

1. Add a `pyproject.toml` (setuptools) so the package installs into `/usr/lib/python3/dist-packages`
   instead of `/usr/share/hangar`, and make `bin/hangar` a console entry point.
2. Create `debian/` with:
   * `control` – `Build-Depends: debhelper-compat (= 13), dh-python, python3-all, pybuild-plugin-pyproject,
     python3-setuptools`; binary `Depends: ${python3:Depends}, ${misc:Depends}, python3-gi, gir1.2-gtk-4.0,
     gir1.2-adw-1 (>= 1.4), pkexec`; `Recommends: python3-apt, gir1.2-appstream-1.0, snapd, flatpak`.
   * `rules` – `dh $@ --with python3 --buildsystem=pybuild`; `override_dh_auto_test` runs `pytest tests`.
   * `install` – helper to `usr/libexec/hangar/hangar-helper`, data files to `usr/share/…`.
   * a generated policy file with `@HELPER_PATH@` replaced by `/usr/libexec/hangar/hangar-helper`.
   * `copyright` (GPL-3.0-or-later), `changelog` (`dch --create`), `source/format` (`3.0 (quilt)`).
3. Build and check: `dpkg-buildpackage -us -uc`, then `lintian` and `appstreamcli validate
   data/io.github.Rajveer_Mahida.Hangar.metainfo.xml`.
4. For a PPA: `debuild -S`, `dput ppa:<you>/<ppa> ../hangar_<version>_source.changes`.

Keep the helper self-contained: it runs as root and must not import modules from writable locations.
