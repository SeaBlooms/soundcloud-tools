"""AI-powered description generation for SoundCloud tracks using Anthropic Claude."""

import json
import os
import time
from pathlib import Path

import anthropic

BASE_DIR = Path(__file__).parent

DEFAULT_GEAR = "Ableton Live, Hardware synths, Hardware drum machines"
DEFAULT_VIBE = (
    "Melancholic & introspective, Hypnotic & repetitive, "
    "Gritty & raw, Bright & euphoric"
)

SYSTEM_PROMPT = """\
You are writing short SoundCloud track descriptions for an electronic music producer.

Artist profile:
- Gear / software: {gear}
- Musical vibe: {vibe}

Rules:
- Write 2-4 sentences max — SoundCloud descriptions should be punchy, not essays
- Be purely descriptive: talk about texture, rhythm, mood, and sonic character
- Do NOT mention influences, other artists, or genre history
- Do NOT use clichés like "take you on a journey" or "sonic landscape"
- Match the tone to the track's mood — dark tracks get darker language, bright ones get lighter
- Reference production qualities where appropriate (e.g. tape hiss, analog warmth, \
clipped drums, sidechained pads) but keep it natural, not a gear list
- If tags or genre are available, let them guide the mood and vocabulary
- Do NOT include the track title in the description
- Return ONLY the description text, no quotes, no labels, no extra formatting"""


def _format_duration(ms: int) -> str:
    total_seconds = ms // 1000
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes}:{seconds:02d}"


def _build_user_prompt(track: dict) -> str:
    parts = [f"Title: {track.get('title', 'Unknown')}"]
    if track.get("genre"):
        parts.append(f"Genre: {track['genre']}")
    if track.get("duration"):
        parts.append(f"Duration: {_format_duration(track['duration'])}")
    if track.get("tag_list"):
        parts.append(f"Tags: {track['tag_list']}")
    if track.get("bpm"):
        parts.append(f"BPM: {track['bpm']}")
    if track.get("description"):
        desc = track["description"][:300]
        parts.append(f"Existing description: {desc}")
    return "\n".join(parts)


class DescriptionGenerator:
    """Generate descriptions for SoundCloud tracks using Anthropic Claude."""

    def __init__(
        self,
        api_key: str | None = None,
        gear: str | None = None,
        vibe: str | None = None,
        model: str = "claude-sonnet-4-20250514",
    ) -> None:
        key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            raise ValueError(
                "ANTHROPIC_API_KEY is required (set in .env or pass explicitly)"
            )
        self.client = anthropic.Anthropic(api_key=key)
        self.model = model
        self.gear = gear or os.environ.get("DESCRIPTION_GEAR", "") or DEFAULT_GEAR
        self.vibe = vibe or os.environ.get("DESCRIPTION_VIBE", "") or DEFAULT_VIBE
        self._system = SYSTEM_PROMPT.format(gear=self.gear, vibe=self.vibe)

    def generate_description(self, track: dict, max_retries: int = 3) -> str:
        """Generate a description for a single track with retry on rate limit."""
        for attempt in range(max_retries):
            try:
                message = self.client.messages.create(
                    model=self.model,
                    max_tokens=400,
                    system=self._system,
                    messages=[
                        {"role": "user", "content": _build_user_prompt(track)}
                    ],
                )
                break
            except anthropic.RateLimitError:
                if attempt == max_retries - 1:
                    raise
                wait = 2 ** (attempt + 1)
                time.sleep(wait)

        text = message.content[0].text.strip()

        if text.startswith('"') and text.endswith('"'):
            text = text[1:-1]

        return text
