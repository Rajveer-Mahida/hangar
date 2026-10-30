"""GitHub release tracking in the UI: update flow, details section, link dialog."""

from __future__ import annotations

import os
import threading

from gi.repository import Adw, GLib, Gtk

from ..core.application_model import AppRecord, UpdateInfo, format_size
from ..services import github_service as gh
from .widgets import esc, format_date, relative_time


def _iso_date(text: str) -> str:
    try:
        return format_date(GLib.DateTime.new_from_iso8601(text, None).to_unix())
    except (TypeError, AttributeError):
        return ""


def open_url(window, url: str) -> None:
    if url.startswith("https://github.com/"):
        Gtk.UriLauncher.new(url).launch(window, None, None)


# ---------------------------------------------------------------------------
# Update flow
# ---------------------------------------------------------------------------

def start_update(window, update: UpdateInfo) -> None:
    """Download a release asset, then hand it to the normal install preview."""
    controller = window.controller
    record = next((controller.inventory.records[k] for k in update.app_keys if k in controller.inventory.records), None)
    if record is None:
        window.toast("This application is no longer in the inventory")
        return
    asset = update.extra.get("asset")
    url = update.extra.get("release_url", "")
    if not asset:
        window.toast("This release has no file for your kind of installation; opening the release page")
        open_url(window, url)
        return
    try:
        _url, filename, size, sha256 = gh.validate_asset(update.package_name, asset, update.extra.get("kind", ""))
    except gh.GitHubError as exc:
        window.show_error("Cannot download this release", str(exc))
        return

    kind = update.extra.get("kind")
    target_dir = os.path.dirname(gh.download_target(record, kind, filename))
    body = (f"{record.name} {update.current_version} → {update.new_version}\n\n"
            f"File: {filename} ({format_size(size)})\nFrom: github.com/{update.package_name} (release "
            f"{update.extra.get('tag', '')})\nSaved to: {target_dir}\n\n")
    body += ("GitHub publishes a checksum for this file; the download is verified against it." if sha256 else
             "This release does not publish a checksum, so the download can only be checked for its size.")
    if kind == "deb":
        body += ("\n\nAfter the download you will see the usual installation preview. The package is installed "
                 "with administrator rights, so only continue if you trust the publisher.")
    else:
        body += "\n\nAfter the download you can review the changes before the old version is replaced."

    def go():
        try:
            op = gh.download_operation(record, update, lambda path: _downloaded(window, record, update, path))
        except gh.GitHubError as exc:
            window.show_error("Cannot download this release", str(exc))
            return
        controller.submit(op)

    dialog = Adw.AlertDialog(heading=f"Download {record.name} {update.new_version}?", body=body)
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("notes", "Release Notes")
    dialog.add_response("download", "Download")
    dialog.set_response_appearance("download", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("download")
    dialog.set_close_response("cancel")

    def on_response(_d, response):
        if response == "download":
            go()
        elif response == "notes":
            open_url(window, url)

    dialog.connect("response", on_response)
    dialog.present(window)


def _downloaded(window, record: AppRecord, update: UpdateInfo, path: str) -> None:
    if update.extra.get("kind") == "deb":
        from .install_dialog import install_file

        install_file(window, path)  # preview, checksum-pinned, authenticated install
        return
    try:
        plan = gh.appimage_replace_plan(record, path)
    except gh.GitHubError as exc:
        window.show_error("The download cannot be used", str(exc))
        return
    lines = [f"New version: {plan['new']}"]
    for launcher in plan["launchers"]:
        lines.append(f"Launcher updated: {launcher}")
    for link in plan["symlinks"]:
        lines.append(f"Link updated: {link}")
    if plan["trash_old"]:
        lines.append(f"Old version moved to the Trash: {plan['old']}")
    else:
        lines.append(f"Old version kept ({plan['old_problem']})")
    dialog = Adw.AlertDialog(heading=f"Switch to {record.name} {update.new_version}?", body="\n".join(lines))
    dialog.add_response("cancel", "Keep Old Version")
    dialog.add_response("switch", "Switch")
    dialog.set_response_appearance("switch", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_close_response("cancel")
    dialog.connect("response", lambda _d, r: r == "switch" and window.controller.submit(
        gh.appimage_replace_operation(record, plan)))
    dialog.present(window)


# ---------------------------------------------------------------------------
# Details page section
# ---------------------------------------------------------------------------

def details_group(window, record: AppRecord) -> Adw.PreferencesGroup | None:
    if not gh.is_trackable(record):
        return None
    controller = window.controller
    stored = controller.db.github_links()
    link = gh.resolve_link(record, stored)
    group = Adw.PreferencesGroup(title="GitHub Releases")
    if not controller.settings.provider_enabled("github"):
        group.set_description("Release checks are turned off in Settings → Integrations.")

    if link is None:
        hint = gh.org_hint(record)
        off = stored.get(gh.link_key(record), ("", ""))[1] == "none"
        subtitle = ("Tracking turned off" if off else
                    f"The homepage points to github.com/{hint}; pick the repository" if hint else
                    "Link the repository this app was downloaded from to be told about new releases")
        row = Adw.ActionRow(title="Not linked", subtitle=esc(subtitle))
        row.add_prefix(Gtk.Image.new_from_icon_name("emblem-shared-symbolic"))
        button = Gtk.Button(label="Link Repository…", valign=Gtk.Align.CENTER)
        button.connect("clicked", lambda *_: LinkRepoDialog(window, record, hint).present(window))
        row.add_suffix(button)
        group.add(row)
        return group

    row = Adw.ActionRow(title=esc(link.repo), subtitle=esc(link.origin.capitalize()), activatable=True)
    row.add_prefix(Gtk.Image.new_from_icon_name("emblem-shared-symbolic"))
    row.add_suffix(Gtk.Image.new_from_icon_name("adw-external-link-symbolic"))
    row.connect("activated", lambda *_: open_url(window, f"https://github.com/{link.repo}/releases"))
    group.add(row)

    summary = gh.latest_release_summary(controller.db, record, link.repo)
    update = controller.inventory.update_for(record)
    if summary:
        text = f"Latest release {summary['tag']}"
        if summary["published_at"]:
            text += f" · {_iso_date(summary['published_at'])}"
        state = ("update available" if update and update.source == "github" else
                 "you have this version or newer" if gh.compare_versions(record.version, summary["tag"]) in (0, 1)
                 else "installed version unknown — compare manually")
        sub = f"{state} · checked {relative_time(summary['checked_at'])}"
        if not summary["asset"] and gh.install_kind(record) != "any":
            sub += " · no matching download in recent releases"
        release_row = Adw.ActionRow(title=esc(text), subtitle=esc(sub))
    else:
        release_row = Adw.ActionRow(title="Not checked yet", subtitle="Use Check Now or wait for the next update check")
    check = Gtk.Button(label="Check Now", valign=Gtk.Align.CENTER)
    check.connect("clicked", lambda *_: window.check_github(record))
    release_row.add_suffix(check)
    group.add(release_row)

    actions = Adw.ActionRow(title="Repository link")
    change = Gtk.Button(label="Change…", valign=Gtk.Align.CENTER)
    change.connect("clicked", lambda *_: LinkRepoDialog(window, record, link.repo.split("/")[0]).present(window))
    actions.add_suffix(change)
    stop = Gtk.Button(label="Stop Tracking", valign=Gtk.Align.CENTER)
    stop.add_css_class("flat")
    stop.connect("clicked", lambda *_: _set_link(window, record, "", "none"))
    actions.add_suffix(stop)
    group.add(actions)
    return group


def _set_link(window, record: AppRecord, repo: str | None, origin: str = "user") -> None:
    controller = window.controller
    controller.db.set_github_link(gh.link_key(record), repo, origin)
    label = f"linked to {repo}" if repo else ("tracking turned off" if origin == "none" else "link reset")
    controller.log_action("settings", f"{record.name} GitHub releases", "success", detail=label)
    window.toast(f"{record.name}: {label}")
    controller.emit("inventory-changed")
    if repo:
        window.check_github(record)
    window.reload_details(record.key)


class LinkRepoDialog(Adw.Dialog):
    """Pick the GitHub repository an app comes from (typed, pasted or searched)."""

    def __init__(self, window, record: AppRecord, hint: str | None = None):
        super().__init__(title=f"Link {record.name} to GitHub", content_width=560, content_height=520)
        self.window = window
        self.record = record
        self._generation = 0
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_start=20, margin_end=20,
                      margin_top=6, margin_bottom=20)
        intro = Gtk.Label(label="Enter the repository this app is released from (owner/name or its GitHub URL), "
                                "or pick one from the search results.", xalign=0, wrap=True)
        intro.add_css_class("dim-label")
        box.append(intro)
        entries = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        entries.add_css_class("boxed-list")
        self.entry = Adw.EntryRow(title="Repository")
        self.entry.connect("changed", lambda *_: self._validate())
        self.entry.connect("entry-activated", lambda *_: self._save())
        entries.append(self.entry)
        box.append(entries)
        self.status = Gtk.Label(xalign=0, wrap=True)
        self.status.add_css_class("caption")
        box.append(self.status)
        self.search = Gtk.SearchEntry(placeholder_text="Search GitHub")
        self.search.set_search_delay(600)
        self.search.connect("search-changed", lambda *_: self._search())
        box.append(self.search)
        self.results = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.results.add_css_class("boxed-list")
        sw = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.set_child(self.results)
        box.append(sw)
        toolbar.set_content(box)
        bottom = Gtk.Box(spacing=12, margin_start=20, margin_end=20, margin_top=10, margin_bottom=16, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        bottom.append(cancel)
        self.save = Gtk.Button(label="Link", sensitive=False)
        self.save.add_css_class("suggested-action")
        self.save.connect("clicked", lambda *_: self._save())
        bottom.append(self.save)
        toolbar.add_bottom_bar(bottom)
        self.set_child(toolbar)

        current = gh.resolve_link(record, window.controller.db.github_links())
        if current:
            self.entry.set_text(current.repo)
        self.name = record.name.split(" (")[0]
        self.search.set_text(f"user:{hint} {self.name}" if hint else self.name)

    def _validate(self) -> str | None:
        repo = gh.normalize_repo(self.entry.get_text())
        self.save.set_sensitive(repo is not None)
        if self.entry.get_text().strip() and repo is None:
            self.status.set_label("Enter owner/name, for example vinifmor/bauh")
        else:
            self.status.set_label("")
        return repo

    def _save(self) -> None:
        repo = self._validate()
        if repo:
            self.close()
            _set_link(self.window, self.record, repo)

    def _search(self) -> None:
        query = self.search.get_text().strip()
        self._generation += 1
        generation = self._generation
        if len(query) < 2:
            return
        self.status.set_label("Searching GitHub…")

        def worker():
            client = gh.GitHubClient()
            try:
                results, error = client.search(query), ""
            except gh.GitHubError as exc:
                results, error = [], str(exc)
                # The owner named by the homepage may not exist publicly: search by name instead.
                if exc.status == 422 and "user:" in query:
                    try:
                        results = client.search(self.name)
                        error = f"{exc} Showing results for “{self.name}” instead." if results else str(exc)
                    except gh.GitHubError as retry:
                        error = str(retry)
            GLib.idle_add(self._show, generation, results, error)

        threading.Thread(target=worker, daemon=True).start()

    def _show(self, generation: int, results: list[dict], error: str) -> bool:
        if generation != self._generation:
            return False
        child = self.results.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.results.remove(child)
            child = nxt
        self.status.set_label(error or ("" if results else "No repositories found"))
        for item in results:
            row = Adw.ActionRow(title=esc(item["repo"]), subtitle=esc(item["description"][:140]), activatable=True)
            stars = Gtk.Label(label=f"★ {item['stars']:,}")
            stars.add_css_class("dim-label")
            row.add_suffix(stars)
            row.connect("activated", lambda _r, repo=item["repo"]: self.entry.set_text(repo))
            self.results.append(row)
        return False
