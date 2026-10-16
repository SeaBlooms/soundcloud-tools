#!/usr/bin/env python3
"""Reorder a SoundCloud playlist's tracks by play count.

Usage:
    python3 reorder_playlist.py https://soundcloud.com/user/sets/playlist
    python3 reorder_playlist.py https://soundcloud.com/user/sets/playlist --dry-run
    python3 reorder_playlist.py https://soundcloud.com/user/sets/playlist --asc
"""

from __future__ import annotations

import sys

from soundcloud_api import SoundCloudClient, load_env_file


def _plays(track: dict) -> int:
    return int(track.get("playback_count") or 0)


def reorder_by_plays(
    sc: SoundCloudClient,
    url: str,
    *,
    ascending: bool = False,
    dry_run: bool = False,
) -> None:
    playlist = sc.resolve_url(url)
    if playlist.get("kind") != "playlist":
        raise RuntimeError(
            f"Resolved URL is a {playlist.get('kind')!r}, not a playlist"
        )

    tracks = playlist.get("tracks") or []
    if not tracks:
        print("Playlist has no tracks.")
        return

    title = playlist.get("title", "(untitled)")
    playlist_id = playlist["id"]
    print(f"\n{'=' * 60}")
    print(f"  {title}  (id={playlist_id})")
    print(f"  {len(tracks)} tracks — sort by plays {'asc' if ascending else 'desc'}")
    print(f"{'=' * 60}\n")

    # Stable sort: ties keep previous relative order.
    indexed = list(enumerate(tracks))
    indexed.sort(key=lambda pair: _plays(pair[1]), reverse=not ascending)
    sorted_tracks = [t for _, t in indexed]

    changed = False
    for new_pos, track in enumerate(sorted_tracks, 1):
        old_pos = next(
            i for i, t in enumerate(tracks, 1) if t["id"] == track["id"]
        )
        marker = " *" if old_pos != new_pos else "  "
        if old_pos != new_pos:
            changed = True
        print(
            f"{marker} {new_pos:2d}. {_plays(track):5d} plays  "
            f"{track.get('title', track['id'])}  (was #{old_pos})"
        )

    if not changed:
        print("\nAlready sorted by plays. Nothing to do.")
        return

    if dry_run:
        print("\n** DRY RUN — no changes pushed **")
        return

    track_ids = [t["id"] for t in sorted_tracks]
    print(f"\nUpdating playlist {playlist_id}…")
    result = sc.update_playlist(playlist_id=playlist_id, track_ids=track_ids)
    print(f"OK — {result.get('track_count', len(track_ids))} tracks reordered.")


def main() -> None:
    load_env_file()

    url: str | None = None
    dry_run = False
    ascending = False

    for arg in sys.argv[1:]:
        if arg == "--dry-run":
            dry_run = True
        elif arg == "--asc":
            ascending = True
        elif arg in ("-h", "--help"):
            print(__doc__)
            return
        elif not arg.startswith("-"):
            url = arg
        else:
            print(f"Unknown flag: {arg}", file=sys.stderr)
            sys.exit(1)

    if not url:
        print("Usage: python3 reorder_playlist.py <playlist-url> [--dry-run] [--asc]")
        sys.exit(1)

    sc = SoundCloudClient()
    reorder_by_plays(sc, url, ascending=ascending, dry_run=dry_run)


if __name__ == "__main__":
    main()
