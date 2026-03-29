"""Spotify Web API integration — currently playing, recent tracks, top items.

Uses spotipy with OAuth2 Authorization Code flow.
Credentials stored in keyring; token cached at TOKEN_FILE_SPOTIFY.
All public functions are async — sync spotipy calls run in asyncio.to_thread().
"""

import asyncio
import logging

import keyring
import spotipy
from spotipy.oauth2 import SpotifyOAuth

from config import KEYRING_SERVICE, TOKEN_FILE_SPOTIFY

log = logging.getLogger("pharoclaw.actions.spotify")

SCOPES = "user-read-currently-playing user-read-recently-played user-top-read"

SKILL = {
    "name": "spotify",
    "description": "Spotify playback — now playing, recently played, top tracks and artists",
    "category": "media",
    "patterns": [
        (r"\b(?:what'?s\s+playing|now\s+playing|current\s+(?:song|track))\b", "spotify_now"),
        (r"\b(?:what\s+am\s+i|what'?s)\s+(?:listening|playing)\b", "spotify_now"),
        (r"\brecently\s+played\b", "spotify_recent"),
        (r"\blistening\s+history\b", "spotify_recent"),
        (r"\btop\s+(?:tracks?|songs?)\b", "spotify_top"),
        (r"\btop\s+artists?\b", "spotify_top"),
        (r"\bmost\s+played\b", "spotify_top"),
    ],
    "actions": [
        {"type": "spotify_now", "handler": "handle_intent", "keywords": "playing listening song track music spotify", "description": "Now playing"},
        {"type": "spotify_recent", "handler": "handle_intent", "keywords": "recently played listening history spotify", "description": "Recently played"},
        {"type": "spotify_top", "handler": "handle_intent", "keywords": "top tracks artists most played spotify", "description": "Top tracks/artists"},
    ],
    "examples": ["What's playing?", "My top artists", "Recently played"],
}


def _get_spotify_client() -> spotipy.Spotify:
    """Build an authenticated Spotify client using keyring credentials."""
    client_id = keyring.get_password(KEYRING_SERVICE, "spotify-client-id")
    client_secret = keyring.get_password(KEYRING_SERVICE, "spotify-client-secret")

    if not client_id or not client_secret:
        raise RuntimeError(
            "Spotify credentials not found in keyring. Set them with:\n"
            f'  keyring set {KEYRING_SERVICE} spotify-client-id\n'
            f'  keyring set {KEYRING_SERVICE} spotify-client-secret'
        )

    auth_manager = SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri="http://localhost:8888/callback",
        scope=SCOPES,
        cache_path=str(TOKEN_FILE_SPOTIFY),
    )
    return spotipy.Spotify(auth_manager=auth_manager)


def _get_now_playing_sync() -> dict | None:
    """Fetch currently playing track. Returns None if nothing is playing."""
    sp = _get_spotify_client()
    result = sp.current_user_playing_track()
    if not result or not result.get("item"):
        return None
    track = result["item"]
    return {
        "name": track["name"],
        "artist": ", ".join(a["name"] for a in track["artists"]),
        "album": track["album"]["name"],
        "is_playing": result.get("is_playing", False),
        "url": track["external_urls"].get("spotify", ""),
    }


def _get_recently_played_sync(limit: int) -> list[dict]:
    """Fetch recently played tracks."""
    sp = _get_spotify_client()
    result = sp.current_user_recently_played(limit=limit)
    tracks = []
    for item in result.get("items", []):
        track = item["track"]
        tracks.append({
            "name": track["name"],
            "artist": ", ".join(a["name"] for a in track["artists"]),
            "played_at": item.get("played_at", ""),
        })
    return tracks


def _get_top_tracks_sync(time_range: str, limit: int) -> list[dict]:
    """Fetch top tracks for the given time range."""
    sp = _get_spotify_client()
    result = sp.current_user_top_tracks(limit=limit, time_range=time_range)
    return [
        {
            "name": track["name"],
            "artist": ", ".join(a["name"] for a in track["artists"]),
            "url": track["external_urls"].get("spotify", ""),
        }
        for track in result.get("items", [])
    ]


def _get_top_artists_sync(time_range: str, limit: int) -> list[dict]:
    """Fetch top artists for the given time range."""
    sp = _get_spotify_client()
    result = sp.current_user_top_artists(limit=limit, time_range=time_range)
    return [
        {
            "name": artist["name"],
            "genres": artist.get("genres", [])[:3],
            "url": artist["external_urls"].get("spotify", ""),
        }
        for artist in result.get("items", [])
    ]


async def get_now_playing() -> dict | None:
    """Get the currently playing track."""
    try:
        return await asyncio.to_thread(_get_now_playing_sync)
    except Exception as e:
        log.error("Failed to get now playing: %s", e)
        return None


async def get_recently_played(limit: int = 10) -> list[dict]:
    """Get recently played tracks."""
    try:
        return await asyncio.to_thread(_get_recently_played_sync, limit)
    except Exception as e:
        log.error("Failed to get recently played: %s", e)
        return []


async def get_top_tracks(time_range: str = "short_term", limit: int = 10) -> list[dict]:
    """Get top tracks. time_range: short_term (4w), medium_term (6mo), long_term (years)."""
    try:
        return await asyncio.to_thread(_get_top_tracks_sync, time_range, limit)
    except Exception as e:
        log.error("Failed to get top tracks: %s", e)
        return []


async def get_top_artists(time_range: str = "short_term", limit: int = 10) -> list[dict]:
    """Get top artists. time_range: short_term (4w), medium_term (6mo), long_term (years)."""
    try:
        return await asyncio.to_thread(_get_top_artists_sync, time_range, limit)
    except Exception as e:
        log.error("Failed to get top artists: %s", e)
        return []


async def handle_intent(action: str, intent: dict, ctx) -> bool:
    """Handle a natural language intent. Returns True if handled."""
    if action == "spotify_now":
        try:
            track = await get_now_playing()
            if not track:
                await ctx.reply("Nothing playing right now.")
            else:
                await ctx.reply(f"\U0001f3b5 Now playing: {track.get('name', '?')} \u2014 {track.get('artist', '?')}"
                                f"\n   Album: {track.get('album', '?')}"
                                + (f"\n   {track['url']}" if track.get('url') else ""))
        except Exception as e:
            await ctx.reply(f"\u274c Spotify failed: {e}")
        return True
    elif action == "spotify_recent":
        try:
            tracks = await get_recently_played(limit=10)
            if not tracks:
                await ctx.reply("No recent listening history.")
            else:
                lines = ["\U0001f3a7 Recently Played:\n"]
                for t in tracks:
                    lines.append(f"  \u2022 {t.get('name', '?')} \u2014 {t.get('artist', '?')}")
                await ctx.reply("\n".join(lines))
        except Exception as e:
            await ctx.reply(f"\u274c Spotify failed: {e}")
        return True
    elif action == "spotify_top":
        try:
            query_text = intent.get("text", "").lower()
            if "artist" in query_text:
                artists = await get_top_artists(limit=10)
                lines = ["\U0001f3a4 Top Artists:\n"]
                for a in artists:
                    lines.append(f"  \u2022 {a.get('name', '?')}")
                await ctx.reply("\n".join(lines))
            else:
                tracks = await get_top_tracks(limit=10)
                lines = ["\U0001f3c6 Top Tracks:\n"]
                for t in tracks:
                    lines.append(f"  \u2022 {t.get('name', '?')} \u2014 {t.get('artist', '?')}")
                await ctx.reply("\n".join(lines))
        except Exception as e:
            await ctx.reply(f"\u274c Spotify failed: {e}")
        return True
    return False
