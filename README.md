# SoundCloud Tools

A suite of terminal-based tools for managing a SoundCloud catalog at scale — AI-powered tagging, description generation, playlist management, and an upcoming automated upload pipeline.

## Tools

### 1. Track Manager (TUI)

An interactive terminal UI for managing tags and descriptions across your entire SoundCloud catalog.

```bash
python3 manage_tracks.py
```

**Features**

- Fetch all tracks from a SoundCloud profile via the API
- AI-powered tag generation — Claude analyzes each track's metadata and produces 10-15 discoverable tags
- AI-powered description generation — short, evocative descriptions tailored to your production style
- Bulk operations — apply and push AI tags or descriptions across your entire catalog at once
- Per-track editor — review and hand-edit tags for individual tracks before pushing
- Smart caching — AI results are cached to disk so re-runs skip already-processed tracks
- CSV export — before/after summary of tag changes

**Keybindings**

| Key | Action |
|-----|--------|
| `f` | Fetch all tracks from your SoundCloud profile |
| `g` | Generate AI tags (skips cached tracks) |
| `d` | Generate AI descriptions (skips cached tracks) |
| `a` | Apply all AI tags — merges AI tags into existing tags for every track |
| `b` | Bulk push AI tags — apply + open push confirmation screen |
| `x` | Bulk push AI descriptions — review old vs new, then push |
| `Enter` | Open per-track editor (edit tags, view AI description) |
| `p` | Push tag changes — review diff and push modified tracks |
| `e` | Export CSV of tag changes |
| `c` | Toggle filter to show only modified tracks |
| `q` | Quit |

**Typical workflow**

```
f → fetch tracks
g → generate AI tags          (cached to .soundcloud_ai_tags.json)
d → generate AI descriptions  (cached to .soundcloud_ai_descriptions.json)
b → bulk push AI tags         (review diff → Push All)
x → bulk push AI descriptions (review old/new → Push All)
```

Progress is logged to `generate_debug.log` (appended each run). Tail it in a separate terminal for live output:

```bash
tail -f generate_debug.log
```

### 2. Playlist Generator (CLI)

A standalone tool that reads a CSV file of playlist definitions and creates or updates them on SoundCloud.

```bash
python3 playlist_generator.py                   # uses playlists.csv
python3 playlist_generator.py custom.csv        # custom CSV file
python3 playlist_generator.py --dry-run         # preview without pushing
```

**CSV format**

```csv
"playlist_key","playlist_title","playlist_description","track_position","track_id","track_title"
"my_playlist","My Playlist Title","Description paragraph 1. |  | Paragraph 2. |  | Tags: tag1, tag2, multi word tag","1","12345","Track Name"
```

| Column | Purpose |
|--------|---------|
| `playlist_key` | Groups rows into the same playlist |
| `playlist_title` | Full title including subtitle |
| `playlist_description` | Paragraphs separated by ` \|  \| `, with an optional `Tags: ...` section extracted into the playlist's tag_list |
| `track_position` | Ordering of tracks within the playlist |
| `track_id` | SoundCloud track ID |
| `track_title` | Track name (for display only) |

The tool matches existing playlists by title — if a match exists it updates; otherwise it creates a new one. Playlists are created with a single track first, then populated via update, to work around SoundCloud's undocumented track-count limit on playlist creation.

### 3. Upload Pipeline (planned)

An automated bi-weekly release pipeline using Google Drive as the source and SoundCloud as the destination. See `.cursor/plans/bi-weekly_upload_pipeline_bdf9aa90.plan.md` for the full architecture.

```bash
python3 pipeline.py scan    # detect new tracks in Drive inbox, generate metadata, approve
python3 pipeline.py queue   # view the release schedule
python3 pipeline.py run     # upload due tracks to SoundCloud (cron-friendly)
```

## Prerequisites

- Python 3.11+
- A [SoundCloud API app](https://developers.soundcloud.com) with Client ID, Client Secret, and `http://localhost:8080/callback` registered as a redirect URI
- An [Anthropic API key](https://console.anthropic.com/) with available credits

## Setup

```bash
git clone <repo-url> && cd soundcloud-tools

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env with your actual keys
```

### `.env` configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `SOUNDCLOUD_CLIENT_ID` | Yes | SoundCloud app client ID |
| `SOUNDCLOUD_CLIENT_SECRET` | Yes | SoundCloud app client secret |
| `SOUNDCLOUD_USER_PERMALINK` | Yes | Your SoundCloud username (e.g. `vanishaudio`) |
| `ANTHROPIC_API_KEY` | Yes | Anthropic API key |
| `SOUNDCLOUD_STYLE_POOL` | No | Comma-separated genres to guide tag generation |
| `DESCRIPTION_GEAR` | No | Your gear/software (defaults to Ableton Live, Hardware synths, Hardware drum machines) |
| `DESCRIPTION_VIBE` | No | Your musical vibe (defaults to Melancholic & introspective, Hypnotic & repetitive, Gritty & raw, Bright & euphoric) |
| `GOOGLE_DRIVE_FOLDER_ID` | No | Google Drive folder ID for the upload pipeline |

## Project Structure

```
soundcloud-tools/
├── manage_tracks.py          # Track Manager TUI (Textual)
├── playlist_generator.py     # Playlist creator/updater from CSV
├── soundcloud_api.py         # SoundCloud API client (OAuth, CRUD, playlists)
├── tag_generator.py          # AI tag generation (Anthropic Claude)
├── description_generator.py  # AI description generation (Anthropic Claude)
├── get_token.py              # Standalone OAuth token test script
├── playlists.csv             # Playlist definitions (input for playlist_generator)
├── requirements.txt          # Python dependencies
├── .env.example              # Template for environment variables
└── .gitignore
```

## Caching

AI results are persisted to local JSON files so re-runs skip already-processed tracks:

- `.soundcloud_ai_tags.json` — generated tags keyed by track ID
- `.soundcloud_ai_descriptions.json` — generated descriptions keyed by track ID

Delete these files to force a full re-generation.

## Authentication

**Read operations** (fetching tracks) use a Client Credentials token — no browser interaction needed.

**Write operations** (pushing tags, descriptions, playlists, uploads) require a user token obtained via Authorization Code + PKCE. The first time you perform a write, a browser window opens for you to authorize the app. The token is cached in `.soundcloud_user_token.json` and refreshed automatically.

test
