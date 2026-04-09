"""SoundCloud API client with two-tier token management.

Read operations (fetch tracks, resolve URLs) use the Client Credentials token
obtained by get_token.py — no browser interaction needed.

Write operations (update tracks) require the Authorization Code + PKCE flow,
which opens a browser for the user to log in and authorize.
"""

import base64
import hashlib
import json
import os
import secrets
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlparse

import requests

BASE_DIR = Path(__file__).parent

AUTHORIZE_URL = "https://secure.soundcloud.com/authorize"
TOKEN_URL = "https://secure.soundcloud.com/oauth/token"
API_BASE = "https://api.soundcloud.com"

APP_TOKEN_FILE = BASE_DIR / ".soundcloud_token.json"
USER_TOKEN_FILE = BASE_DIR / ".soundcloud_user_token.json"
REDIRECT_PORT = 8080
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"


def load_env_file(path: Path = BASE_DIR / ".env") -> None:
    if not path.exists():
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def _token_is_valid(token_data: dict) -> bool:
    obtained_at = token_data.get("obtained_at", 0)
    expires_in = token_data.get("expires_in", 0)
    return time.time() < (obtained_at + expires_in - 60)


def _load_token_file(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _save_token_file(path: Path, token_data: dict) -> None:
    token_data["obtained_at"] = int(time.time())
    path.write_text(json.dumps(token_data, indent=2) + "\n")


def _generate_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


class _CallbackHandler(BaseHTTPRequestHandler):
    auth_code: str | None = None
    state: str | None = None

    def do_GET(self) -> None:
        qs = parse_qs(urlparse(self.path).query)
        _CallbackHandler.auth_code = qs.get("code", [None])[0]
        _CallbackHandler.state = qs.get("state", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(
            b"<html><body><h2>Authorization complete</h2>"
            b"<p>You can close this tab and return to the terminal.</p>"
            b"</body></html>"
        )

    def log_message(self, format: str, *args: Any) -> None:
        pass


class SoundCloudClient:
    """SoundCloud API client with separate read (app) and write (user) tokens."""

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
    ) -> None:
        load_env_file()
        self.client_id = client_id or os.environ.get("SOUNDCLOUD_CLIENT_ID", "")
        self.client_secret = client_secret or os.environ.get("SOUNDCLOUD_CLIENT_SECRET", "")
        if not self.client_id or not self.client_secret:
            raise ValueError(
                "SOUNDCLOUD_CLIENT_ID and SOUNDCLOUD_CLIENT_SECRET are required"
            )
        self._app_token: dict | None = None
        self._user_token: dict | None = None

    # ------------------------------------------------------------------
    # App token (Client Credentials) — for read operations
    # ------------------------------------------------------------------

    def _request_app_token(self) -> dict:
        resp = requests.post(
            TOKEN_URL,
            data={"grant_type": "client_credentials"},
            auth=(self.client_id, self.client_secret),
            headers={"Accept": "application/json; charset=utf-8"},
        )
        resp.raise_for_status()
        token_data = resp.json()
        _save_token_file(APP_TOKEN_FILE, token_data)
        self._app_token = token_data
        return token_data

    def _refresh_app_token(self) -> dict:
        if not self._app_token or not self._app_token.get("refresh_token"):
            return self._request_app_token()
        resp = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self._app_token["refresh_token"],
            },
            headers={"Accept": "application/json; charset=utf-8"},
        )
        if not resp.ok:
            return self._request_app_token()
        token_data = resp.json()
        _save_token_file(APP_TOKEN_FILE, token_data)
        self._app_token = token_data
        return token_data

    def ensure_app_token(self) -> str:
        """Return a valid client credentials token, refreshing or requesting as needed."""
        if self._app_token and _token_is_valid(self._app_token):
            return self._app_token["access_token"]

        cached = _load_token_file(APP_TOKEN_FILE)
        if cached:
            self._app_token = cached
            if _token_is_valid(cached):
                return cached["access_token"]
            return self._refresh_app_token()["access_token"]

        return self._request_app_token()["access_token"]

    # ------------------------------------------------------------------
    # User token (Authorization Code + PKCE) — for write operations
    # ------------------------------------------------------------------

    def _refresh_user_token(self) -> dict:
        if not self._user_token or not self._user_token.get("refresh_token"):
            raise RuntimeError("No refresh token — re-authorize required")
        resp = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self._user_token["refresh_token"],
            },
            headers={"Accept": "application/json; charset=utf-8"},
        )
        resp.raise_for_status()
        token_data = resp.json()
        _save_token_file(USER_TOKEN_FILE, token_data)
        self._user_token = token_data
        return token_data

    def ensure_user_token(self) -> str:
        """Return a valid user token, refreshing or authorizing as needed."""
        if self._user_token and _token_is_valid(self._user_token):
            return self._user_token["access_token"]

        cached = _load_token_file(USER_TOKEN_FILE)
        if cached:
            self._user_token = cached
            if _token_is_valid(cached):
                return cached["access_token"]
            if cached.get("refresh_token"):
                return self._refresh_user_token()["access_token"]

        return self.authorize()["access_token"]

    @property
    def has_user_token(self) -> bool:
        cached = _load_token_file(USER_TOKEN_FILE)
        if cached:
            self._user_token = cached
        return self._user_token is not None and _token_is_valid(self._user_token)

    def authorize(self) -> dict:
        """Run the full browser-based Authorization Code + PKCE flow."""
        state = secrets.token_urlsafe(32)
        verifier, challenge = _generate_pkce()

        _CallbackHandler.auth_code = None
        _CallbackHandler.state = None

        server = HTTPServer(("127.0.0.1", REDIRECT_PORT), _CallbackHandler)
        server_thread = Thread(target=server.handle_request, daemon=True)
        server_thread.start()

        params = {
            "client_id": self.client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
        auth_url = f"{AUTHORIZE_URL}?{urlencode(params)}"
        webbrowser.open(auth_url)

        server_thread.join(timeout=120)
        server.server_close()

        if not _CallbackHandler.auth_code:
            raise RuntimeError("Authorization timed out — no code received")
        if _CallbackHandler.state != state:
            raise RuntimeError("State mismatch — possible CSRF attack")

        resp = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "redirect_uri": REDIRECT_URI,
                "code_verifier": verifier,
                "code": _CallbackHandler.auth_code,
            },
            headers={"Accept": "application/json; charset=utf-8"},
        )
        resp.raise_for_status()
        token_data = resp.json()
        _save_token_file(USER_TOKEN_FILE, token_data)
        self._user_token = token_data
        return token_data

    # ------------------------------------------------------------------
    # API helpers
    # ------------------------------------------------------------------

    def _read_headers(self) -> dict[str, str]:
        token = self.ensure_app_token()
        return {
            "Authorization": f"OAuth {token}",
            "Accept": "application/json; charset=utf-8",
        }

    def _write_headers(self) -> dict[str, str]:
        token = self.ensure_user_token()
        return {
            "Authorization": f"OAuth {token}",
            "Accept": "application/json; charset=utf-8",
        }

    # ------------------------------------------------------------------
    # Read operations (use app/client-credentials token)
    # ------------------------------------------------------------------

    def resolve_url(self, url: str) -> dict:
        """Resolve a SoundCloud URL to its API resource."""
        if not url.startswith("http"):
            url = f"https://soundcloud.com/{url}"
        resp = requests.get(
            f"{API_BASE}/resolve",
            params={"url": url},
            headers=self._read_headers(),
        )
        resp.raise_for_status()
        return resp.json()

    def get_user_tracks(
        self,
        user_id: int,
        on_progress: Callable[[int], None] | None = None,
    ) -> list[dict]:
        """Fetch all tracks for a user by ID (read-only, uses app token)."""
        tracks: list[dict] = []
        params: dict[str, Any] = {"limit": 200, "linked_partitioning": "true"}
        url: str | None = f"{API_BASE}/users/{user_id}/tracks"

        while url:
            resp = requests.get(url, params=params, headers=self._read_headers())
            resp.raise_for_status()
            data = resp.json()
            batch = data.get("collection", [])
            tracks.extend(batch)
            if on_progress:
                on_progress(len(tracks))
            url = data.get("next_href")
            params = {}

        return tracks

    def get_my_tracks(
        self,
        on_progress: Callable[[int], None] | None = None,
    ) -> list[dict]:
        """Fetch all tracks for the authenticated user (requires user token)."""
        tracks: list[dict] = []
        params: dict[str, Any] = {"limit": 200, "linked_partitioning": "true"}
        url: str | None = f"{API_BASE}/me/tracks"

        while url:
            resp = requests.get(url, params=params, headers=self._write_headers())
            resp.raise_for_status()
            data = resp.json()
            batch = data.get("collection", [])
            tracks.extend(batch)
            if on_progress:
                on_progress(len(tracks))
            url = data.get("next_href")
            params = {}

        return tracks

    # ------------------------------------------------------------------
    # Write operations (use user/authorization-code token)
    # ------------------------------------------------------------------

    def update_track_tags(self, track_id: int, tag_list: str) -> dict:
        """Update a track's tags (requires user token)."""
        resp = requests.put(
            f"{API_BASE}/tracks/{track_id}",
            json={"track": {"tag_list": tag_list}},
            headers={
                **self._write_headers(),
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
        return resp.json()

    def update_track_description(self, track_id: int, description: str) -> dict:
        """Update a track's description (requires user token)."""
        resp = requests.put(
            f"{API_BASE}/tracks/{track_id}",
            json={"track": {"description": description}},
            headers={
                **self._write_headers(),
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Playlist operations (require user token)
    # ------------------------------------------------------------------

    def get_my_playlists(self) -> list[dict]:
        """Fetch all playlists for the authenticated user."""
        playlists: list[dict] = []
        params: dict[str, Any] = {"limit": 200, "linked_partitioning": "true"}
        url: str | None = f"{API_BASE}/me/playlists"

        while url:
            resp = requests.get(url, params=params, headers=self._write_headers())
            resp.raise_for_status()
            data = resp.json()
            playlists.extend(data.get("collection", []))
            url = data.get("next_href")
            params = {}

        return playlists

    def create_playlist(
        self,
        title: str,
        track_ids: list[int],
        description: str = "",
        tag_list: str = "",
        sharing: str = "public",
    ) -> dict:
        """Create a new playlist (requires user token)."""
        payload: dict[str, Any] = {
            "playlist": {
                "title": title,
                "sharing": sharing,
                "tracks": [{"id": str(tid)} for tid in track_ids],
            }
        }
        if description:
            payload["playlist"]["description"] = description
        if tag_list:
            payload["playlist"]["tag_list"] = tag_list

        resp = requests.post(
            f"{API_BASE}/playlists",
            json=payload,
            headers=self._write_headers(),
        )
        if not resp.ok:
            raise RuntimeError(
                f"{resp.status_code} {resp.reason}: {resp.text[:500]}"
            )
        return resp.json()

    def update_playlist(
        self,
        playlist_id: int,
        title: str | None = None,
        track_ids: list[int] | None = None,
        description: str | None = None,
        tag_list: str | None = None,
    ) -> dict:
        """Update an existing playlist (requires user token)."""
        inner: dict[str, Any] = {}
        if title is not None:
            inner["title"] = title
        if description is not None:
            inner["description"] = description
        if tag_list is not None:
            inner["tag_list"] = tag_list
        if track_ids is not None:
            inner["tracks"] = [{"id": str(tid)} for tid in track_ids]

        resp = requests.put(
            f"{API_BASE}/playlists/{playlist_id}",
            json={"playlist": inner},
            headers=self._write_headers(),
        )
        if not resp.ok:
            raise RuntimeError(
                f"{resp.status_code} {resp.reason}: {resp.text[:500]}"
            )
        return resp.json()

    # ------------------------------------------------------------------
    # User info
    # ------------------------------------------------------------------

    def get_me(self) -> dict:
        return requests.get(
            f"{API_BASE}/me", headers=self._write_headers()
        ).json()
