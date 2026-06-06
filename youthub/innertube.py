"""InnerTube client — speaks YouTube's internal API as a TVHTML5 device.

Why TVHTML5: it's the simplest client surface — less anti-bot friction,
fewer DRM hoops, and responses use compact tile-based renderers that
are easy to parse. Real TV apps (Cobalt, PlayStation, Apple TV) use it.

Why Bearer (OAuth) instead of cookies: ties requests to the user's
Google account so recommendations and watch history are personalized.
Tokens come from auth.py's Device Authorization Grant flow.

The clientVersion below is bumped every few months by Google. If
endpoints start returning empty payloads or 400s, refresh the version
to whatever current PlayStation/AppleTV YouTube apps send.
"""
from __future__ import annotations

import json
from typing import Any, Optional

import httpx

from . import auth

BASE = "https://www.youtube.com/youtubei/v1"

CLIENT_NAME = "TVHTML5"
CLIENT_VERSION = "7.20250122.14.00"
CLIENT_NAME_HEADER = "7"  # X-YouTube-Client-Name for TVHTML5

# InnerTube search filter: results type «Videos» (works for TV + WEB bodies).
SEARCH_PARAMS_VIDEOS = "EgIQAQ=="

USER_AGENT = (
    "Mozilla/5.0 (PlayStation; PlayStation 4/12.50) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/13.1.2 Safari/605.1.15"
)


def _context() -> dict:
    return {
        "client": {
            "clientName": CLIENT_NAME,
            "clientVersion": CLIENT_VERSION,
            "platform": "TV",
            "clientFormFactor": "UNKNOWN_FORM_FACTOR",
            "browserName": "Cobalt",
            "browserVersion": "1.0",
            "osName": "Tizen",
            "osVersion": "8.0",
            "userAgent": USER_AGENT,
            # ru/RU so YT returns original Russian titles instead of
            # auto-translating them into English. English videos still
            # show their original English titles. Side effect: views/
            # date strings ("просмотров", "1 год назад") and YT's
            # default text fields come back in Russian.
            "hl": "ru",
            "gl": "RU",
            "utcOffsetMinutes": 0,
        },
        "user": {"lockedSafetyMode": False},
        "request": {"useSsl": True},
    }


class InnerTube:
    """Authenticated InnerTube session.

    Use one instance per app run — keeps an HTTP/2 connection alive
    and tracks the Bearer token, refreshing as needed.
    """

    def __init__(self, tokens: Optional[auth.Tokens] = None):
        self._tokens = tokens or auth.get_tokens()
        self._http = httpx.Client(
            http2=True,
            timeout=30.0,
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/json",
                "X-YouTube-Client-Name": CLIENT_NAME_HEADER,
                "X-YouTube-Client-Version": CLIENT_VERSION,
                "Origin": "https://www.youtube.com",
                "Referer": "https://www.youtube.com/",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "InnerTube":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _ensure_fresh(self) -> None:
        if not self._tokens.is_fresh():
            self._tokens = auth.refresh(self._tokens)

    def _post(self, endpoint: str, body: dict) -> dict:
        self._ensure_fresh()
        full = {"context": _context(), **body}
        headers = {"Authorization": f"Bearer {self._tokens.access_token}"}
        r = self._http.post(
            f"{BASE}/{endpoint}",
            headers=headers,
            content=json.dumps(full).encode(),
        )
        if r.status_code == 401:
            # access_token rejected — force a refresh and retry once
            self._tokens = auth.refresh(self._tokens)
            headers["Authorization"] = f"Bearer {self._tokens.access_token}"
            r = self._http.post(
                f"{BASE}/{endpoint}",
                headers=headers,
                content=json.dumps(full).encode(),
            )
        r.raise_for_status()
        return r.json()

    # --- endpoint wrappers -------------------------------------------------

    def browse(self, browse_id: str, *, params: Optional[str] = None,
               continuation: Optional[str] = None) -> dict:
        body: dict[str, Any] = {}
        if continuation:
            body["continuation"] = continuation
        else:
            body["browseId"] = browse_id
            if params:
                body["params"] = params
        return self._post("browse", body)

    def search(self, query: str, *, params: Optional[str] = None,
               continuation: Optional[str] = None) -> dict:
        body: dict[str, Any] = {}
        if continuation:
            body["continuation"] = continuation
        else:
            body["query"] = query
            body["params"] = params if params is not None else SEARCH_PARAMS_VIDEOS
        return self._post("search", body)

    def search_web(self, query: str, *, params: Optional[str] = None,
                   continuation: Optional[str] = None) -> dict:
        """Search with WEB client context — richer results than bare TV search."""
        self._ensure_fresh()
        body: dict[str, Any] = {}
        if continuation:
            body["continuation"] = continuation
        else:
            body["query"] = query
            body["params"] = params if params is not None else SEARCH_PARAMS_VIDEOS
        ctx = {
            "client": {
                "clientName": "WEB",
                "clientVersion": "2.20250222.10.00",
                "hl": "ru",
                "gl": "RU",
                "userAgent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
                ),
            },
            "user": {"lockedSafetyMode": False},
            "request": {"useSsl": True},
        }
        full = {"context": ctx, **body}
        headers = {"Authorization": f"Bearer {self._tokens.access_token}"}
        r = self._http.post(
            f"{BASE}/search",
            headers=headers,
            content=json.dumps(full).encode(),
        )
        if r.status_code == 401:
            self._tokens = auth.refresh(self._tokens)
            headers["Authorization"] = f"Bearer {self._tokens.access_token}"
            r = self._http.post(
                f"{BASE}/search",
                headers=headers,
                content=json.dumps(full).encode(),
            )
        r.raise_for_status()
        return r.json()

    def next(self, video_id: str, *,
             continuation: Optional[str] = None) -> dict:
        body: dict[str, Any] = {}
        if continuation:
            body["continuation"] = continuation
        else:
            body["videoId"] = video_id
        return self._post("next", body)

    def player(self, video_id: str) -> dict:
        """Authenticated /player call. We use it only to get a
        watchtimeUrl that is server-side bound to the user's account —
        sabr_bridge still fetches its own PR for streaming, this one
        is for stats attribution. Bearer-authed so the playbackTracking
        URLs include the correct session params for the signed-in
        Google account, not an anonymous visitor."""
        return self._post("player", {"videoId": video_id})

    # convenience -----------------------------------------------------------

    def home(self) -> dict:
        """TVHTML5 home / 'What to Watch' surface."""
        return self.browse("FEwhat_to_watch")

    def subscriptions(self) -> dict:
        return self.browse("FEsubscriptions")
