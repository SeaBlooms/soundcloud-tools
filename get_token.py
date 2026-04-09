#!/usr/bin/env python3
"""Obtain a SoundCloud OAuth access token via the Client Credentials flow.

Usage:
    # With environment variables
    export SOUNDCLOUD_CLIENT_ID=your_id
    export SOUNDCLOUD_CLIENT_SECRET=your_secret
    python3 get_token.py

    # With a .env file containing the above variables
    python3 get_token.py

    # Force a fresh token (ignore cached)
    python3 get_token.py --fresh

    # Refresh an existing token
    python3 get_token.py --refresh
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

TOKEN_URL = "https://secure.soundcloud.com/oauth/token"
TOKEN_FILE = Path(__file__).parent / ".soundcloud_token.json"


def load_env_file(path: Path = Path(__file__).parent / ".env") -> None:
    """Load key=value pairs from a .env file into os.environ."""
    if not path.exists():
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip().strip("\"'")
            os.environ.setdefault(key.strip(), value)


def get_credentials() -> tuple[str, str]:
    load_env_file()
    client_id = os.environ.get("SOUNDCLOUD_CLIENT_ID", "").strip()
    client_secret = os.environ.get("SOUNDCLOUD_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        print(
            "Error: SOUNDCLOUD_CLIENT_ID and SOUNDCLOUD_CLIENT_SECRET must be set\n"
            "  via environment variables or a .env file in this directory.",
            file=sys.stderr,
        )
        sys.exit(1)
    return client_id, client_secret


def post_token_request(
    data: dict,
    auth: tuple[str, str] | None = None,
) -> dict:
    resp = requests.post(
        TOKEN_URL,
        data=data,
        auth=auth,
        headers={"Accept": "application/json; charset=utf-8"},
    )
    if not resp.ok:
        print(f"HTTP {resp.status_code}: {resp.reason}", file=sys.stderr)
        try:
            print(json.dumps(resp.json(), indent=2), file=sys.stderr)
        except (ValueError, requests.JSONDecodeError):
            print(resp.text, file=sys.stderr)
        sys.exit(1)
    return resp.json()


def request_client_credentials_token(client_id: str, client_secret: str) -> dict:
    return post_token_request(
        data={"grant_type": "client_credentials"},
        auth=(client_id, client_secret),
    )


def refresh_access_token(
    client_id: str, client_secret: str, refresh_token: str
) -> dict:
    return post_token_request(
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        },
    )


def save_token(token_data: dict) -> None:
    token_data["obtained_at"] = int(time.time())
    TOKEN_FILE.write_text(json.dumps(token_data, indent=2) + "\n")
    print(f"Token saved to {TOKEN_FILE}")


def load_cached_token() -> dict | None:
    if not TOKEN_FILE.exists():
        return None
    try:
        return json.loads(TOKEN_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def token_is_valid(token_data: dict) -> bool:
    obtained_at = token_data.get("obtained_at", 0)
    expires_in = token_data.get("expires_in", 0)
    return time.time() < (obtained_at + expires_in - 60)


def print_token_info(token_data: dict) -> None:
    print(f"\naccess_token:  {token_data['access_token']}")
    if token_data.get("refresh_token"):
        print(f"refresh_token: {token_data['refresh_token']}")
    expires_in = token_data.get("expires_in", 0)
    obtained_at = token_data.get("obtained_at", int(time.time()))
    remaining = max(0, obtained_at + expires_in - int(time.time()))
    print(f"expires_in:    {expires_in}s ({remaining}s remaining)")
    if token_data.get("scope"):
        print(f"scope:         {token_data['scope']}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Obtain a SoundCloud OAuth access token (Client Credentials flow)"
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--fresh",
        action="store_true",
        help="Force a new token request, ignoring any cached token",
    )
    group.add_argument(
        "--refresh",
        action="store_true",
        help="Refresh the cached token using its refresh_token",
    )
    args = parser.parse_args()

    client_id, client_secret = get_credentials()

    if args.refresh:
        cached = load_cached_token()
        if not cached or not cached.get("refresh_token"):
            print("No cached refresh_token found. Requesting a new token.", file=sys.stderr)
            token_data = request_client_credentials_token(client_id, client_secret)
        else:
            print("Refreshing token...")
            token_data = refresh_access_token(
                client_id, client_secret, cached["refresh_token"]
            )
        save_token(token_data)
        print_token_info(token_data)
        return

    if not args.fresh:
        cached = load_cached_token()
        if cached and token_is_valid(cached):
            print("Using cached token (still valid).")
            print_token_info(cached)
            return

    print("Requesting new client credentials token...")
    token_data = request_client_credentials_token(client_id, client_secret)
    save_token(token_data)
    print_token_info(token_data)


if __name__ == "__main__":
    main()
