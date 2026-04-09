"""AI-powered tag generation for SoundCloud tracks using Anthropic Claude."""

import json
import os
import time
from pathlib import Path
from typing import Any, Callable

import anthropic

BASE_DIR = Path(__file__).parent

DEFAULT_STYLE_POOL = (
    "IDM,Electronica,Downtempo,Lo-Fi,House,UKG,Breaks,Techno,"
    "Vaporwave,Synthwave"
)

SYSTEM_PROMPT = """\
You are a SoundCloud tag optimization expert. Given metadata about a music track, \
generate 10-15 highly relevant tags that will maximize discoverability.

Tag categories to include:
- Genre tags (from the artist's style pool provided)
- Mood/atmosphere tags (e.g. dreamy, dark, uplifting, melancholic)
- Production style tags (e.g. glitchy, sample-heavy, analog, layered)
- Scene/community tags (e.g. underground, bedroom producer, netlabel)
- Tempo/energy tags when relevant (e.g. slow burn, high energy)

Rules:
- Only return tags that genuinely fit the track based on its metadata
- Prefer lowercase tags
- Multi-word tags are fine (they'll be quoted automatically)
- Do NOT repeat the track title or artist name as a tag
- Return ONLY a JSON array of strings, no other text"""


def _format_duration(ms: int) -> str:
    total_seconds = ms // 1000
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes}:{seconds:02d}"


def _build_user_prompt(track: dict, style_pool: str) -> str:
    parts = [f"Artist's style pool: {style_pool}\n"]
    parts.append(f"Title: {track.get('title', 'Unknown')}")
    if track.get("genre"):
        parts.append(f"Genre: {track['genre']}")
    if track.get("description"):
        desc = track["description"][:500]
        parts.append(f"Description: {desc}")
    if track.get("duration"):
        parts.append(f"Duration: {_format_duration(track['duration'])}")
    if track.get("tag_list"):
        parts.append(f"Current tags: {track['tag_list']}")
    if track.get("bpm"):
        parts.append(f"BPM: {track['bpm']}")
    return "\n".join(parts)


class TagGenerator:
    """Generate tags for SoundCloud tracks using Anthropic Claude."""

    def __init__(
        self,
        api_key: str | None = None,
        style_pool: str | None = None,
        model: str = "claude-sonnet-4-20250514",
    ) -> None:
        key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            raise ValueError(
                "ANTHROPIC_API_KEY is required (set in .env or pass explicitly)"
            )
        self.client = anthropic.Anthropic(api_key=key)
        self.model = model
        self.style_pool = (
            style_pool
            or os.environ.get("SOUNDCLOUD_STYLE_POOL", "")
            or DEFAULT_STYLE_POOL
        )

    def generate_tags(self, track: dict, max_retries: int = 3) -> list[str]:
        """Generate tags for a single track with retry on rate limit."""
        for attempt in range(max_retries):
            try:
                message = self.client.messages.create(
                    model=self.model,
                    max_tokens=300,
                    system=SYSTEM_PROMPT,
                    messages=[
                        {"role": "user", "content": _build_user_prompt(track, self.style_pool)}
                    ],
                )
                break
            except anthropic.RateLimitError:
                if attempt == max_retries - 1:
                    raise
                wait = 2 ** (attempt + 1)
                time.sleep(wait)

        text = message.content[0].text.strip()

        # Handle cases where the model wraps the JSON in markdown code fences
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.startswith("```")]
            text = "\n".join(lines)

        try:
            tags = json.loads(text)
        except json.JSONDecodeError:
            tags = [t.strip().strip('"') for t in text.split(",") if t.strip()]

        return [str(t) for t in tags if t]

    def generate_tags_batch(
        self,
        tracks: list[dict],
        on_progress: Callable[[int, int, dict], None] | None = None,
    ) -> dict[int, list[str]]:
        """Generate tags for multiple tracks.

        Returns {track_id: [tags]}. Calls on_progress(completed, total, track)
        after each track is processed.
        """
        results: dict[int, list[str]] = {}
        total = len(tracks)

        for i, track in enumerate(tracks):
            track_id = track["id"]
            try:
                tags = self.generate_tags(track)
                results[track_id] = tags
            except Exception as exc:
                results[track_id] = [f"ERROR: {exc}"]
            if on_progress:
                on_progress(i + 1, total, track)
            if i < total - 1:
                time.sleep(1.0)

        return results
