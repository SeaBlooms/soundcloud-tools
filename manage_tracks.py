#!/usr/bin/env python3
"""SoundCloud Track Manager — interactive TUI for managing track tags & descriptions."""

import csv
import json
import os
import shlex
from datetime import datetime
from pathlib import Path
from typing import Iterable

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Static,
    TextArea,
)

from description_generator import DescriptionGenerator
from soundcloud_api import SoundCloudClient, load_env_file
from tag_generator import TagGenerator

BASE_DIR = Path(__file__).parent
AI_TAGS_CACHE = BASE_DIR / ".soundcloud_ai_tags.json"
AI_DESCS_CACHE = BASE_DIR / ".soundcloud_ai_descriptions.json"


# ---------------------------------------------------------------------------
# AI cache helpers
# ---------------------------------------------------------------------------

def _load_json_cache(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_json_cache(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n")


def load_ai_tags_cache() -> dict[int, list[str]]:
    raw = _load_json_cache(AI_TAGS_CACHE)
    return {int(k): v for k, v in raw.items()}


def save_ai_tags_cache(ai_tags: dict[int, list[str]]) -> None:
    _save_json_cache(AI_TAGS_CACHE, {str(k): v for k, v in ai_tags.items()})


def load_ai_descs_cache() -> dict[int, str]:
    raw = _load_json_cache(AI_DESCS_CACHE)
    return {int(k): v for k, v in raw.items()}


def save_ai_descs_cache(descs: dict[int, str]) -> None:
    _save_json_cache(AI_DESCS_CACHE, {str(k): v for k, v in descs.items()})


# ---------------------------------------------------------------------------
# Tag format helpers
# ---------------------------------------------------------------------------

def parse_tag_list(tag_string: str) -> list[str]:
    """Parse SoundCloud's space-separated tag string (with quoted multi-word tags)."""
    if not tag_string or not tag_string.strip():
        return []
    try:
        return shlex.split(tag_string)
    except ValueError:
        return tag_string.split()


def format_tag_list(tags: Iterable[str]) -> str:
    """Convert a list back to SoundCloud's space-separated format."""
    parts = []
    for tag in tags:
        tag = tag.strip()
        if not tag:
            continue
        parts.append(f'"{tag}"' if " " in tag else tag)
    return " ".join(parts)


def _fmt_duration(ms: int | None) -> str:
    if not ms:
        return "--:--"
    total = ms // 1000
    m, s = divmod(total, 60)
    return f"{m}:{s:02d}"


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------

class TrackListScreen(Screen):
    """Main screen — DataTable of all tracks with keybindings for every action."""

    BINDINGS = [
        Binding("f", "fetch_tracks", "Fetch Tracks", priority=True),
        Binding("g", "generate_tags", "Generate Tags", priority=True),
        Binding("d", "generate_descs", "Generate Descs", priority=True),
        Binding("a", "apply_all_ai", "Apply All AI", priority=True),
        Binding("b", "bulk_push_ai", "Bulk Push AI", priority=True),
        Binding("x", "bulk_push_descs", "Bulk Push Descs", priority=True),
        Binding("enter", "edit_track", "Edit Track", priority=True),
        Binding("p", "push_changes", "Push Changes", priority=True),
        Binding("e", "export_csv", "Export CSV", priority=True),
        Binding("c", "toggle_filter", "Filter Changed", priority=True),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="track-table")
        yield Static(
            "Press [b]f[/b] to fetch tracks | No tracks loaded",
            id="status-bar",
        )
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.add_columns("Title", "Genre", "Dur", "Current Tags", "AI Tags", "Status")

    # --- table helpers (main thread only) ---

    def _update_status(self, text: str) -> None:
        self.query_one("#status-bar", Static).update(text)

    def _refresh_table(self) -> None:
        app: TagManagerApp = self.app  # type: ignore[assignment]
        table = self.query_one(DataTable)
        table.clear()

        tracks = app.tracks
        if app.show_changed_only:
            tracks = [t for t in tracks if app.is_track_modified(t)]

        for track in tracks:
            tid = track["id"]
            current = app.get_track_tags(track)
            ai = app.ai_tags.get(tid, [])
            modified = app.is_track_modified(track)
            has_desc = tid in app.ai_descs

            parts = []
            if modified:
                parts.append("Tags✎")
            elif ai:
                parts.append("Tags✓")
            if has_desc:
                parts.append("Desc✓")
            status = " | ".join(parts) or "—"

            cur_str = ", ".join(current)[:50] or "—"
            ai_str = ", ".join(ai)[:50] or "—"

            table.add_row(
                track.get("title", "?")[:40],
                track.get("genre", "—")[:15],
                _fmt_duration(track.get("duration")),
                cur_str,
                ai_str,
                status,
                key=str(tid),
            )

        changed = len(app.get_modified_tracks())
        filt = " (changed only)" if app.show_changed_only else ""
        self._update_status(
            f"Tracks: {len(app.tracks)}{filt} | "
            f"Modified: {changed} | AI tags: {len(app.ai_tags)} | "
            f"AI descs: {len(app.ai_descs)}"
        )

    # --- actions ---

    def action_fetch_tracks(self) -> None:
        self._cached_app = self.app  # type: ignore[assignment]
        self._do_fetch()

    @work(thread=True, exclusive=True, group="fetch")
    def _do_fetch(self) -> None:
        app: TagManagerApp = self._cached_app
        update_status = self._update_status
        refresh_table = self._refresh_table
        try:
            if not app.sc:
                app.call_from_thread(update_status, "Connecting to SoundCloud…")
                app.sc = SoundCloudClient()

            permalink = os.environ.get("SOUNDCLOUD_USER_PERMALINK", "").strip()
            if not permalink:
                app.call_from_thread(
                    update_status,
                    "[red]Set SOUNDCLOUD_USER_PERMALINK in .env (your SoundCloud username)[/red]",
                )
                return

            app.call_from_thread(update_status, "Resolving user…")
            user = app.sc.resolve_url(permalink)
            app.user_id = user["id"]

            def on_progress(count: int) -> None:
                app.call_from_thread(
                    update_status, f"Fetching tracks… ({count} so far)"
                )

            app.tracks = app.sc.get_user_tracks(app.user_id, on_progress=on_progress)
            app.call_from_thread(refresh_table)
        except Exception as exc:
            app.call_from_thread(update_status, f"[red]Error: {exc}[/red]")

    def action_generate_tags(self) -> None:
        app: TagManagerApp = self.app  # type: ignore[assignment]
        if not app.tracks:
            self._update_status("No tracks loaded — press [b]f[/b] first")
            return
        self._update_status(f"Starting AI tag generation for {len(app.tracks)} tracks…")
        self.notify(f"Generating tags for {len(app.tracks)} tracks…", timeout=5)
        # Capture app ref on the main thread — self.app uses a ContextVar
        # that is NOT available inside worker threads
        self._cached_app = app
        self._do_generate()

    @work(thread=True)
    def _do_generate(self) -> None:
        import time
        import traceback
        from datetime import datetime as _dt

        app: TagManagerApp = self._cached_app
        log = BASE_DIR / "generate_debug.log"

        def _log(msg: str) -> None:
            with open(log, "a") as f:
                f.write(f"[{_dt.now().strftime('%H:%M:%S')}] {msg}\n")

        try:
            if not app.tagger:
                _log("Creating TagGenerator")
                app.tagger = TagGenerator()

            uncached = [
                t for t in app.tracks
                if t["id"] not in app.ai_tags
                or (app.ai_tags[t["id"]] and app.ai_tags[t["id"]][0].startswith("ERROR"))
            ]

            if not uncached:
                _log(f"All {len(app.tracks)} tracks already cached — skipping")
                app.call_from_thread(self._refresh_table)
                app.call_from_thread(
                    self.notify, "All tracks already have cached AI tags.", timeout=5
                )
                return

            total = len(uncached)
            skipped = len(app.tracks) - total
            _log(f"Starting generation: {total} uncached, {skipped} cached/skipped")
            app.call_from_thread(
                self._update_status,
                f"Generating tags for {total} uncached tracks (skipping {skipped} cached)…",
            )

            for i, track in enumerate(uncached, 1):
                title = track.get("title", "?")[:30]
                try:
                    tags = app.tagger.generate_tags(track)
                    app.ai_tags[track["id"]] = tags
                    _log(f"  {i}/{total} OK: {title}")
                except Exception as exc:
                    app.ai_tags[track["id"]] = [f"ERROR: {exc}"]
                    _log(f"  {i}/{total} FAIL: {title} — {exc}")

                app.call_from_thread(
                    self._update_status,
                    f"Generating tags: {i}/{total} — {title}",
                )
                if i < total:
                    time.sleep(1.0)

            save_ai_tags_cache(app.ai_tags)
            _log(f"Done — {len(app.ai_tags)} total cached tags saved to disk")
            app.call_from_thread(self._refresh_table)
            app.call_from_thread(
                self.notify,
                f"Done! Generated tags for {total} tracks (cached to disk).",
                timeout=5,
            )
        except Exception as exc:
            _log(f"FATAL: {traceback.format_exc()}")
            app.call_from_thread(
                self._update_status, f"[red]Error: {exc}[/red]"
            )
            app.call_from_thread(
                self.notify, f"Error: {exc}", severity="error", timeout=10
            )

    def action_edit_track(self) -> None:
        app: TagManagerApp = self.app  # type: ignore[assignment]
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        track_id = int(row_key.value)
        track = next((t for t in app.tracks if t["id"] == track_id), None)
        if track:
            self.app.push_screen(
                TagEditorScreen(track), callback=self._on_editor_dismiss
            )

    def _on_editor_dismiss(self, _result: object = None) -> None:
        self._refresh_table()

    def action_push_changes(self) -> None:
        app: TagManagerApp = self.app  # type: ignore[assignment]
        if not app.get_modified_tracks():
            self._update_status("No modified tracks to push")
            return
        self.app.push_screen(PushScreen(), callback=self._on_push_dismiss)

    def _on_push_dismiss(self, _result: object = None) -> None:
        self._refresh_table()

    def action_export_csv(self) -> None:
        app: TagManagerApp = self.app  # type: ignore[assignment]
        if not app.tracks:
            self._update_status("No tracks to export")
            return

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = BASE_DIR / f"tag_changes_{ts}.csv"

        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["track_id", "title", "old_tags", "new_tags", "status"])
            for track in app.tracks:
                original = parse_tag_list(track.get("tag_list", ""))
                current = app.get_track_tags(track)
                writer.writerow([
                    track["id"],
                    track.get("title", ""),
                    ", ".join(original),
                    ", ".join(current),
                    "modified" if app.is_track_modified(track) else "unchanged",
                ])

        self._update_status(f"Exported to {path.name}")

    def action_toggle_filter(self) -> None:
        app: TagManagerApp = self.app  # type: ignore[assignment]
        app.show_changed_only = not app.show_changed_only
        self._refresh_table()

    @staticmethod
    def _merge_tags(existing: list[str], ai: list[str]) -> list[str]:
        """Append AI tags to existing tags, deduped case-insensitively."""
        seen = {t.lower() for t in existing}
        merged = list(existing)
        for tag in ai:
            if tag.lower() not in seen:
                seen.add(tag.lower())
                merged.append(tag)
        return merged

    def action_apply_all_ai(self) -> None:
        """Stage merged (existing + AI) tags as pending edits (without pushing)."""
        app: TagManagerApp = self.app  # type: ignore[assignment]
        if not app.ai_tags:
            self._update_status("No AI tags available — press [b]g[/b] first")
            return
        applied = 0
        for track in app.tracks:
            tid = track["id"]
            ai = app.ai_tags.get(tid, [])
            if ai and not ai[0].startswith("ERROR"):
                existing = app.get_track_tags(track)
                app.edited_tags[tid] = self._merge_tags(existing, ai)
                applied += 1
        self._refresh_table()
        self.notify(
            f"Merged AI tags into {applied} track(s). Press [b]p[/b] to review & push.",
            timeout=5,
        )

    def action_bulk_push_ai(self) -> None:
        """Merge AI tags into existing tags and jump straight to the Push screen."""
        app: TagManagerApp = self.app  # type: ignore[assignment]
        if not app.ai_tags:
            self._update_status("No AI tags available — press [b]g[/b] first")
            return
        applied = 0
        for track in app.tracks:
            tid = track["id"]
            ai = app.ai_tags.get(tid, [])
            if ai and not ai[0].startswith("ERROR"):
                existing = app.get_track_tags(track)
                app.edited_tags[tid] = self._merge_tags(existing, ai)
                applied += 1
        if applied == 0:
            self._update_status("No valid AI tags to push")
            return
        self._refresh_table()
        self.app.push_screen(PushScreen(), callback=self._on_push_dismiss)

    # --- description generation ---

    def action_generate_descs(self) -> None:
        app: TagManagerApp = self.app  # type: ignore[assignment]
        if not app.tracks:
            self._update_status("No tracks loaded — press [b]f[/b] first")
            return
        self._update_status(f"Starting AI description generation for {len(app.tracks)} tracks…")
        self.notify(f"Generating descriptions for {len(app.tracks)} tracks…", timeout=5)
        self._cached_app = app
        self._do_generate_descs()

    @work(thread=True)
    def _do_generate_descs(self) -> None:
        import time
        import traceback
        from datetime import datetime as _dt

        app: TagManagerApp = self._cached_app
        log = BASE_DIR / "generate_debug.log"

        def _log(msg: str) -> None:
            with open(log, "a") as f:
                f.write(f"[{_dt.now().strftime('%H:%M:%S')}] [desc] {msg}\n")

        try:
            if not app.desc_gen:
                _log("Creating DescriptionGenerator")
                app.desc_gen = DescriptionGenerator()

            uncached = [
                t for t in app.tracks
                if t["id"] not in app.ai_descs
                or app.ai_descs[t["id"]].startswith("ERROR")
            ]

            if not uncached:
                _log(f"All {len(app.tracks)} descriptions already cached — skipping")
                app.call_from_thread(self._refresh_table)
                app.call_from_thread(
                    self.notify, "All tracks already have cached AI descriptions.", timeout=5
                )
                return

            total = len(uncached)
            skipped = len(app.tracks) - total
            _log(f"Starting description generation: {total} uncached, {skipped} cached")
            app.call_from_thread(
                self._update_status,
                f"Generating descs for {total} uncached tracks (skipping {skipped} cached)…",
            )

            for i, track in enumerate(uncached, 1):
                title = track.get("title", "?")[:30]
                try:
                    desc = app.desc_gen.generate_description(track)
                    app.ai_descs[track["id"]] = desc
                    _log(f"  {i}/{total} OK: {title}")
                except Exception as exc:
                    app.ai_descs[track["id"]] = f"ERROR: {exc}"
                    _log(f"  {i}/{total} FAIL: {title} — {exc}")

                app.call_from_thread(
                    self._update_status,
                    f"Generating descs: {i}/{total} — {title}",
                )
                if i < total:
                    time.sleep(1.0)

            save_ai_descs_cache(app.ai_descs)
            _log(f"Done — {len(app.ai_descs)} total cached descriptions saved")
            app.call_from_thread(self._refresh_table)
            app.call_from_thread(
                self.notify,
                f"Done! Generated descriptions for {total} tracks (cached to disk).",
                timeout=5,
            )
        except Exception as exc:
            _log(f"FATAL [desc]: {traceback.format_exc()}")
            app.call_from_thread(
                self._update_status, f"[red]Error: {exc}[/red]"
            )
            app.call_from_thread(
                self.notify, f"Error: {exc}", severity="error", timeout=10
            )

    def action_bulk_push_descs(self) -> None:
        """Push AI descriptions for all tracks."""
        app: TagManagerApp = self.app  # type: ignore[assignment]
        if not app.ai_descs:
            self._update_status("No AI descriptions available — press [b]d[/b] first")
            return
        valid = {
            tid: desc for tid, desc in app.ai_descs.items()
            if not desc.startswith("ERROR")
        }
        if not valid:
            self._update_status("No valid AI descriptions to push")
            return
        self.app.push_screen(
            DescriptionPushScreen(), callback=self._on_push_dismiss
        )


class TagEditorScreen(Screen):
    """Per-track tag editor with AI suggestions."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, track: dict) -> None:
        super().__init__()
        self.track = track
        self.track_id = track["id"]

    def compose(self) -> ComposeResult:
        app: TagManagerApp = self.app  # type: ignore[assignment]

        title = self.track.get("title", "Unknown")
        genre = self.track.get("genre", "—")
        dur = _fmt_duration(self.track.get("duration"))
        desc = (self.track.get("description") or "—")[:200]

        yield Header()

        yield Static(
            f"[b]{title}[/b]\n"
            f"Genre: {genre}  |  Duration: {dur}\n"
            f"Description: {desc}",
            id="editor-info",
        )

        current = app.get_track_tags(self.track)
        yield Label("Tags (one per line):")
        yield TextArea("\n".join(current), id="editor-tags")

        ai = app.ai_tags.get(self.track_id, [])
        if ai:
            yield Static(
                f"[b]AI Suggestions:[/b]  {', '.join(ai)}",
                id="editor-suggestions",
            )

        ai_desc = app.ai_descs.get(self.track_id, "")
        if ai_desc and not ai_desc.startswith("ERROR"):
            yield Static(
                f"[b]AI Description:[/b]\n{ai_desc}",
                id="editor-ai-desc",
            )

        with Horizontal(id="custom-tag-row"):
            yield Input(
                placeholder="Type a tag and press Enter to add",
                id="custom-tag-input",
            )
            if ai:
                yield Button("Add All AI", id="add-ai-btn", variant="success")

        with Horizontal(id="editor-actions"):
            yield Button("Save", id="save-btn", variant="primary")
            yield Button("Cancel", id="cancel-btn", variant="default")

        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        app: TagManagerApp = self.app  # type: ignore[assignment]

        if event.button.id == "save-btn":
            textarea = self.query_one("#editor-tags", TextArea)
            tags = [t.strip() for t in textarea.text.split("\n") if t.strip()]
            app.edited_tags[self.track_id] = tags
            self.dismiss(True)

        elif event.button.id == "cancel-btn":
            self.dismiss(False)

        elif event.button.id == "add-ai-btn":
            textarea = self.query_one("#editor-tags", TextArea)
            existing = {
                t.strip().lower() for t in textarea.text.split("\n") if t.strip()
            }
            ai = app.ai_tags.get(self.track_id, [])
            new_tags = [t for t in ai if t.lower() not in existing]
            if new_tags:
                current_text = textarea.text.rstrip("\n")
                sep = "\n" if current_text else ""
                textarea.load_text(current_text + sep + "\n".join(new_tags))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "custom-tag-input":
            tag = event.value.strip()
            if tag:
                textarea = self.query_one("#editor-tags", TextArea)
                current_text = textarea.text.rstrip("\n")
                sep = "\n" if current_text else ""
                textarea.load_text(current_text + sep + tag)
                event.input.value = ""

    def action_cancel(self) -> None:
        self.dismiss(False)


class PushScreen(Screen):
    """Shows a diff of pending changes and pushes them to SoundCloud."""

    BINDINGS = [
        Binding("escape", "go_back", "Back"),
    ]

    def compose(self) -> ComposeResult:
        app: TagManagerApp = self.app  # type: ignore[assignment]
        modified = app.get_modified_tracks()

        yield Header()
        yield Label(f"[b]Push Changes — {len(modified)} track(s)[/b]")

        with VerticalScroll(id="push-list"):
            for track in modified:
                tid = track["id"]
                title = track.get("title", "?")
                original = set(parse_tag_list(track.get("tag_list", "")))
                current = set(app.edited_tags.get(tid, []))
                added = sorted(current - original)
                removed = sorted(original - current)

                lines = [f"[b]{title}[/b]"]
                if added:
                    lines.append(f"  [green]+ {', '.join(added)}[/green]")
                if removed:
                    lines.append(f"  [red]- {', '.join(removed)}[/red]")
                yield Static("\n".join(lines))

        with Horizontal(id="push-actions"):
            yield Button("Push All", id="push-btn", variant="warning")
            yield Button("Back", id="back-btn", variant="default")

        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "push-btn":
            self._cached_app = self.app  # type: ignore[assignment]
            self._do_push()
        elif event.button.id == "back-btn":
            self.dismiss(None)

    @work(thread=True, exclusive=True, group="push")
    def _do_push(self) -> None:
        import time

        app: TagManagerApp = self._cached_app
        modified = app.get_modified_tracks()
        total = len(modified)
        errors = 0

        for i, track in enumerate(modified, 1):
            tid = track["id"]
            title = track.get("title", "?")[:30]
            tags = app.edited_tags[tid]
            tag_string = format_tag_list(tags)

            app.call_from_thread(
                self.notify,
                f"Pushing {i}/{total}: {title}…",
                timeout=3,
            )

            try:
                app.sc.update_track_tags(tid, tag_string)
                track["tag_list"] = tag_string
            except Exception as exc:
                errors += 1
                app.call_from_thread(
                    self.notify,
                    f"Error on {title}: {exc}",
                    severity="error",
                )

            if i < total:
                time.sleep(0.5)

        for track in modified:
            app.edited_tags.pop(track["id"], None)

        msg = f"Pushed {total - errors}/{total} track(s)"
        if errors:
            msg += f" ({errors} failed)"
        app.call_from_thread(self.notify, msg, severity="information")
        app.call_from_thread(self.dismiss, None)

    def action_go_back(self) -> None:
        self.dismiss(None)


class DescriptionPushScreen(Screen):
    """Review and push AI-generated descriptions to SoundCloud."""

    BINDINGS = [
        Binding("escape", "go_back", "Back"),
    ]

    def compose(self) -> ComposeResult:
        app: TagManagerApp = self.app  # type: ignore[assignment]
        pending = [
            t for t in app.tracks
            if t["id"] in app.ai_descs
            and not app.ai_descs[t["id"]].startswith("ERROR")
        ]

        yield Header()
        yield Label(f"[b]Push Descriptions — {len(pending)} track(s)[/b]")

        with VerticalScroll(id="push-list"):
            for track in pending:
                tid = track["id"]
                title = track.get("title", "?")
                old = (track.get("description") or "").strip()
                new = app.ai_descs[tid]
                old_preview = (old[:120] + "…") if len(old) > 120 else (old or "[empty]")
                new_preview = (new[:120] + "…") if len(new) > 120 else new

                yield Static(
                    f"[b]{title}[/b]\n"
                    f"  [red]Old:[/red] {old_preview}\n"
                    f"  [green]New:[/green] {new_preview}"
                )

        with Horizontal(id="push-actions"):
            yield Button("Push All", id="push-btn", variant="warning")
            yield Button("Back", id="back-btn", variant="default")

        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "push-btn":
            self._cached_app = self.app  # type: ignore[assignment]
            self._do_push()
        elif event.button.id == "back-btn":
            self.dismiss(None)

    @work(thread=True, exclusive=True, group="push-descs")
    def _do_push(self) -> None:
        import time

        app: TagManagerApp = self._cached_app
        pending = [
            t for t in app.tracks
            if t["id"] in app.ai_descs
            and not app.ai_descs[t["id"]].startswith("ERROR")
        ]
        total = len(pending)
        errors = 0

        for i, track in enumerate(pending, 1):
            tid = track["id"]
            title = track.get("title", "?")[:30]
            desc = app.ai_descs[tid]

            app.call_from_thread(
                self.notify,
                f"Pushing desc {i}/{total}: {title}…",
                timeout=3,
            )

            try:
                app.sc.update_track_description(tid, desc)
                track["description"] = desc
            except Exception as exc:
                errors += 1
                app.call_from_thread(
                    self.notify,
                    f"Error on {title}: {exc}",
                    severity="error",
                )

            if i < total:
                time.sleep(0.5)

        msg = f"Pushed descriptions for {total - errors}/{total} track(s)"
        if errors:
            msg += f" ({errors} failed)"
        app.call_from_thread(self.notify, msg, severity="information")
        app.call_from_thread(self.dismiss, None)

    def action_go_back(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class TagManagerApp(App):
    """SoundCloud Tag Manager TUI."""

    TITLE = "SoundCloud Track Manager"

    CSS = """
    Screen {
        background: $surface;
    }

    #track-table {
        height: 1fr;
    }

    #status-bar {
        dock: bottom;
        height: 1;
        background: $accent;
        color: $text;
        padding: 0 1;
    }

    #editor-info {
        height: auto;
        max-height: 8;
        padding: 1;
        background: $panel;
        border: tall $accent;
    }

    #editor-tags {
        height: 1fr;
        min-height: 8;
    }

    #editor-suggestions {
        height: auto;
        max-height: 6;
        padding: 1;
        background: $panel;
        border: tall $success;
    }

    #editor-ai-desc {
        height: auto;
        max-height: 8;
        padding: 1;
        background: $panel;
        border: tall $warning;
    }

    #custom-tag-row {
        height: 3;
        layout: horizontal;
    }

    #custom-tag-input {
        width: 1fr;
    }

    #add-ai-btn {
        width: auto;
        min-width: 14;
    }

    #editor-actions {
        dock: bottom;
        height: 3;
        layout: horizontal;
        align: center middle;
    }

    #editor-actions Button {
        margin: 0 1;
    }

    #push-list {
        height: 1fr;
        padding: 1;
    }

    #push-actions {
        dock: bottom;
        height: 3;
        layout: horizontal;
        align: center middle;
    }

    #push-actions Button {
        margin: 0 1;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.sc: SoundCloudClient | None = None
        self.tagger: TagGenerator | None = None
        self.desc_gen: DescriptionGenerator | None = None
        self.tracks: list[dict] = []
        self.ai_tags: dict[int, list[str]] = load_ai_tags_cache()
        self.ai_descs: dict[int, str] = load_ai_descs_cache()
        self.edited_tags: dict[int, list[str]] = {}
        self.show_changed_only = False
        self.user_id: int | None = None

    def on_mount(self) -> None:
        self.push_screen(TrackListScreen())

    def get_track_tags(self, track: dict) -> list[str]:
        tid = track["id"]
        if tid in self.edited_tags:
            return self.edited_tags[tid]
        return parse_tag_list(track.get("tag_list", ""))

    def is_track_modified(self, track: dict) -> bool:
        tid = track["id"]
        if tid not in self.edited_tags:
            return False
        original = set(parse_tag_list(track.get("tag_list", "")))
        edited = set(self.edited_tags[tid])
        return original != edited

    def get_modified_tracks(self) -> list[dict]:
        return [t for t in self.tracks if self.is_track_modified(t)]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    load_env_file()
    TagManagerApp().run()


if __name__ == "__main__":
    main()
