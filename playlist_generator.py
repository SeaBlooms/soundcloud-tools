#!/usr/bin/env python3
"""Read a playlist CSV and create / update SoundCloud playlists.

Usage:
    python3 playlist_generator.py                   # uses playlists.csv
    python3 playlist_generator.py my_playlists.csv  # custom file
    python3 playlist_generator.py --dry-run          # preview without pushing
"""

import csv
import re
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from soundcloud_api import SoundCloudClient, load_env_file

BASE_DIR = Path(__file__).parent
DEFAULT_CSV = BASE_DIR / "playlists.csv"


@dataclass
class PlaylistSpec:
    key: str
    title: str
    description: str
    tag_list: str
    track_ids: list[int] = field(default_factory=list)
    track_titles: list[str] = field(default_factory=list)


def _parse_description(raw: str) -> tuple[str, str]:
    """Split raw CSV description into (clean description, tag_list string).

    The CSV uses ' |  | ' as paragraph separators and embeds a
    'Tags: ...' section at the end.
    """
    sections = [s.strip() for s in raw.split("|") if s.strip()]

    body_parts: list[str] = []
    tags_str = ""

    for section in sections:
        if section.lower().startswith("tags:"):
            tag_csv = section[len("tags:"):].strip()
            tags = [t.strip() for t in tag_csv.split(",") if t.strip()]
            parts = []
            for t in tags:
                parts.append(f'"{t}"' if " " in t else t)
            tags_str = " ".join(parts)
        else:
            body_parts.append(section)

    description = "\n\n".join(body_parts)
    return description, tags_str


def load_playlists(csv_path: Path) -> list[PlaylistSpec]:
    """Parse the CSV into a list of PlaylistSpec objects, one per unique playlist."""
    playlists: OrderedDict[str, PlaylistSpec] = OrderedDict()

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            key = row["playlist_key"]
            if key not in playlists:
                desc, tag_list = _parse_description(row["playlist_description"])
                playlists[key] = PlaylistSpec(
                    key=key,
                    title=row["playlist_title"],
                    description=desc,
                    tag_list=tag_list,
                )
            playlists[key].track_ids.append(int(row["track_id"]))
            playlists[key].track_titles.append(row["track_title"])

    return list(playlists.values())


def print_summary(specs: list[PlaylistSpec]) -> None:
    print(f"\n{'=' * 60}")
    print(f"  Playlist Generator — {len(specs)} playlist(s)")
    print(f"{'=' * 60}\n")

    for spec in specs:
        print(f"  [{spec.key}] {spec.title}")
        print(f"    Tracks: {len(spec.track_ids)}")
        print(f"    Tags:   {spec.tag_list[:80]}{'…' if len(spec.tag_list) > 80 else ''}")
        print(f"    Desc:   {spec.description[:100]}{'…' if len(spec.description) > 100 else ''}")
        track_list = ", ".join(spec.track_titles[:5])
        if len(spec.track_titles) > 5:
            track_list += f" … +{len(spec.track_titles) - 5} more"
        print(f"    Songs:  {track_list}")
        print()


def _create_and_populate(sc: SoundCloudClient, spec: PlaylistSpec) -> dict:
    """Create with 1 track, then update with full track list + metadata.

    SoundCloud's POST /playlists silently caps the number of tracks
    you can include at creation time (~20). Work around it by creating
    with a single track, then PUTting the full payload.
    """
    result = sc.create_playlist(
        title=spec.title,
        track_ids=[spec.track_ids[0]],
    )
    time.sleep(1.0)
    sc.update_playlist(
        playlist_id=result["id"],
        description=spec.description,
        tag_list=spec.tag_list,
        track_ids=spec.track_ids,
    )
    return result


def push_playlists(
    sc: SoundCloudClient,
    specs: list[PlaylistSpec],
    dry_run: bool = False,
) -> None:
    """Create or update playlists on SoundCloud."""
    if dry_run:
        for i, spec in enumerate(specs, 1):
            print(f"  [{i}/{len(specs)}] {spec.title} ({len(spec.track_ids)} tracks) — dry run, skipped")
        print(f"\nDone (dry run).")
        return

    print("Fetching existing playlists…")
    existing = sc.get_my_playlists()
    by_title = {p["title"]: p for p in existing}
    print(f"  Found {len(existing)} existing playlist(s)\n")

    for i, spec in enumerate(specs, 1):
        match = by_title.get(spec.title)
        action = "UPDATE" if match else "CREATE"
        print(f"  [{i}/{len(specs)}] {action}: {spec.title} ({len(spec.track_ids)} tracks)")

        try:
            if match:
                sc.update_playlist(
                    playlist_id=match["id"],
                    title=spec.title,
                    description=spec.description,
                    tag_list=spec.tag_list,
                    track_ids=spec.track_ids,
                )
            else:
                _create_and_populate(sc, spec)
            print(f"    OK")
        except Exception as exc:
            print(f"    ERROR: {exc}")

        if i < len(specs):
            time.sleep(3.0)

    print(f"\nDone.")


def test_create(sc: SoundCloudClient, csv_path: Path) -> None:
    """Diagnostic: isolate what causes the 422 on specific playlists."""
    import requests as req

    specs = load_playlists(csv_path)
    spec = specs[0]  # Deep Hours — the first failing playlist

    def _try(label: str, title: str, track_ids: list[int]) -> int | None:
        print(f"\n  Test: {label}")
        print(f"    title={title[:60]}  tracks={len(track_ids)}")
        try:
            result = sc.create_playlist(title=title, track_ids=track_ids)
            pid = result["id"]
            print(f"    SUCCESS (id={pid}) — deleting…")
            req.delete(
                f"https://api.soundcloud.com/playlists/{pid}",
                headers=sc._write_headers(),
            )
            return pid
        except Exception as exc:
            print(f"    FAILED: {exc}")
            return None

    print("--- Diagnostic: isolating 422 cause ---")
    time.sleep(1)

    # Test 1: ASCII title + 1 track
    _try("ASCII title + 1 track", "test_playlist", [spec.track_ids[0]])
    time.sleep(2)

    # Test 2: Real title (em dash) + 1 track
    _try("Real title + 1 track", spec.title, [spec.track_ids[0]])
    time.sleep(2)

    # Test 3: ASCII title + all tracks
    _try("ASCII title + all tracks", "test_playlist", spec.track_ids)
    time.sleep(2)

    # Test 4: Real title + all tracks
    _try("Real title + all tracks", spec.title, spec.track_ids)
    time.sleep(2)

    # Test 5: ASCII-safe title (em dash → dash) + all tracks
    safe_title = spec.title.replace("—", "-")
    _try("Safe title (dash) + all tracks", safe_title, spec.track_ids)


def main() -> None:
    load_env_file()

    csv_path = DEFAULT_CSV
    dry_run = False
    run_test = False

    for arg in sys.argv[1:]:
        if arg == "--dry-run":
            dry_run = True
        elif arg == "--test":
            run_test = True
        elif not arg.startswith("-"):
            csv_path = Path(arg)

    if not csv_path.exists() and not run_test:
        print(f"Error: CSV file not found: {csv_path}")
        sys.exit(1)

    sc = SoundCloudClient()

    if run_test:
        test_create(sc, csv_path)
        return

    specs = load_playlists(csv_path)
    print_summary(specs)

    if dry_run:
        print("  ** DRY RUN — no changes will be made **\n")
        push_playlists(None, specs, dry_run=True)  # type: ignore[arg-type]
        return

    push_playlists(sc, specs)


if __name__ == "__main__":
    main()
