"""Small reusable widgets and formatting helpers."""

from __future__ import annotations

import os
import time
from datetime import datetime

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from ..core.application_model import AppRecord, Confidence, Source, format_size

SOURCE_ICONS = {
    Source.APT: "package-x-generic-symbolic",
    Source.VENDOR: "emblem-system-symbolic",
    Source.SNAP: "application-x-addon-symbolic",
    Source.FLATPAK: "system-software-install-symbolic",
    Source.APPIMAGE: "application-x-executable-symbolic",
    Source.MANUAL: "folder-symbolic",
    Source.UNKNOWN: "dialog-question-symbolic",
}

FALLBACK_ICON = "application-x-executable"


class LazyRefresh:
    """Mixin for pages: rebuild only while visible and once per burst of changes.

    Inventory, update and history signals often arrive several at a time;
    rebuilding every page (hidden ones included) for each of them blocked the
    main thread and made scrolling stutter. Pages call ``queue_refresh()``
    instead of ``refresh()``; hidden pages just remember they are stale.
    """

    def init_lazy_refresh(self) -> None:
        self._lazy_dirty = False
        self._lazy_source = 0
        self.connect("map", lambda *_: self._lazy_schedule())

    def queue_refresh(self, *_args) -> None:
        self._lazy_dirty = True
        self._lazy_schedule()

    def _lazy_schedule(self) -> None:
        if self._lazy_dirty and not self._lazy_source and self.get_mapped():
            self._lazy_source = GLib.idle_add(self._lazy_run)

    def _lazy_run(self) -> bool:
        self._lazy_source = 0
        if self._lazy_dirty and self.get_mapped():
            self._lazy_dirty = False
            self.refresh()
        return False


def esc(text: str | None) -> str:
    """Escape text for widgets that interpret Pango markup (e.g. Adw rows)."""
    return GLib.markup_escape_text(text or "")


def css_source(source: Source | str) -> str:
    value = source.value if isinstance(source, Source) else source
    return f"source-{value}"


def source_badge(source: Source) -> Gtk.Label:
    label = Gtk.Label(label=source.label, valign=Gtk.Align.CENTER)
    label.add_css_class("source-badge")
    label.add_css_class(css_source(source))
    return label


def set_source_badge(label: Gtk.Label, source: Source) -> None:
    for s in Source:
        label.remove_css_class(css_source(s))
    label.set_label(source.label)
    label.add_css_class(css_source(source))


def pill(text: str, kind: str = "neutral") -> Gtk.Label:
    label = Gtk.Label(label=text, valign=Gtk.Align.CENTER)
    label.add_css_class("tag")
    label.add_css_class(f"tag-{kind}")
    return label


def confidence_pill(confidence: Confidence) -> Gtk.Label:
    label = Gtk.Label(label=confidence.label, valign=Gtk.Align.CENTER)
    label.add_css_class("tag")
    label.add_css_class(f"confidence-{confidence.value}")
    return label


_theme_cache: dict[str, bool] = {}
_PIXMAP_DIRS = ("/usr/share/pixmaps", "/usr/local/share/pixmaps")


def _pixmap(icon: str) -> str:
    """GTK 4 no longer searches pixmaps directories, but many launchers rely on them."""
    for directory in _PIXMAP_DIRS:
        for ext in ("", ".png", ".svg", ".xpm"):
            candidate = os.path.join(directory, icon + ext)
            if os.path.isfile(candidate):
                return candidate
    return ""


def gicon_for(icon: str) -> Gio.Icon:
    """Gio.Icon for an icon name or absolute path, with a generic fallback."""
    if icon and os.path.isabs(icon):
        if os.path.isfile(icon):
            return Gio.FileIcon.new(Gio.File.new_for_path(icon))
        icon = ""
    if icon:
        display = Gdk.Display.get_default()
        if display is not None:
            if icon not in _theme_cache:
                theme = Gtk.IconTheme.get_for_display(display)
                _theme_cache[icon] = theme.has_icon(icon)
            if _theme_cache[icon]:
                return Gio.ThemedIcon.new(icon)
        pixmap = _pixmap(icon)
        if pixmap:
            return Gio.FileIcon.new(Gio.File.new_for_path(pixmap))
        # Icon names like "foo.png" are sometimes written in launchers.
        stripped = os.path.splitext(icon)[0]
        if stripped != icon:
            return gicon_for(stripped)
    return Gio.ThemedIcon.new(FALLBACK_ICON)


def app_image(record: AppRecord | None = None, size: int = 48) -> Gtk.Image:
    image = Gtk.Image(pixel_size=size)
    image.add_css_class("icon-dropshadow")
    if record is not None:
        image.set_from_gicon(gicon_for(record.icon))
    return image


def format_date(ts: float | None, with_time: bool = False) -> str:
    if not ts:
        return ""
    dt = datetime.fromtimestamp(ts)
    if with_time:
        return dt.strftime("%d %b %Y, %H:%M")
    return dt.strftime("%d %b %Y")


def relative_time(ts: float | None) -> str:
    if not ts:
        return "never"
    delta = time.time() - ts
    if delta < 0:
        return format_date(ts)
    if delta < 60:
        return "just now"
    if delta < 3600:
        m = int(delta // 60)
        return f"{m} minute{'s' if m != 1 else ''} ago"
    if delta < 86400:
        h = int(delta // 3600)
        return f"{h} hour{'s' if h != 1 else ''} ago"
    if delta < 86400 * 14:
        d = int(delta // 86400)
        return f"{d} day{'s' if d != 1 else ''} ago"
    return format_date(ts)


def size_text(size: int | None) -> str:
    return format_size(size) if size else ""


def copy_to_clipboard(widget: Gtk.Widget, text: str) -> None:
    widget.get_clipboard().set_content(Gdk.ContentProvider.new_for_value(text))


def empty_state(icon: str, title: str, description: str = "") -> Adw.StatusPage:
    page = Adw.StatusPage(icon_name=icon, title=title, description=description)
    page.set_vexpand(True)
    return page


def section_label(text: str) -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=0)
    label.add_css_class("section-title")
    return label


def clear_box(box: Gtk.Widget) -> None:
    """Remove all children of a Gtk.Box, Gtk.ListBox or Gtk.FlowBox."""
    child = box.get_first_child()
    while child is not None:
        nxt = child.get_next_sibling()
        box.remove(child)
        child = nxt


def icon_label(icon: str, text: str, css: str | None = None) -> Gtk.Box:
    box = Gtk.Box(spacing=6)
    box.append(Gtk.Image.new_from_icon_name(icon))
    label = Gtk.Label(label=text, xalign=0)
    if css:
        label.add_css_class(css)
    box.append(label)
    return box


def path_exists(path: str) -> bool:
    return bool(path) and os.path.lexists(path)


def scrolled(child: Gtk.Widget, clamp: bool = True, maximum: int = 1100) -> Gtk.ScrolledWindow:
    sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
    if clamp:
        c = Adw.Clamp(maximum_size=maximum, tightening_threshold=maximum - 200)
        c.set_child(child)
        sw.set_child(c)
    else:
        sw.set_child(child)
    return sw
