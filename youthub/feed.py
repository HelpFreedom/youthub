"""Parse InnerTube TVHTML5 responses into flat dataclasses.

The raw JSON is a deeply nested renderer tree. UI code shouldn't care
about that — it just wants `feed.shelves[0].videos[0].title`.

Robustness: many fields are optional in YouTube responses (badges,
durations, channel names sometimes missing on shorts/livestreams).
We default to None / empty rather than crashing.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Optional


# --- raw helpers -----------------------------------------------------------


def _text(node: Optional[dict]) -> Optional[str]:
    """Extract plain text from a YouTube text node — handles simpleText / runs."""
    if not isinstance(node, dict):
        return None
    if "simpleText" in node:
        return node["simpleText"]
    runs = node.get("runs")
    if isinstance(runs, list):
        return "".join(r.get("text", "") for r in runs if isinstance(r, dict))
    return None


def _best_thumbnail(thumbs: list) -> Optional[str]:
    """Pick the largest thumbnail URL. Thumbs are ordered small→large already
    but we don't rely on that — explicit max-by-area."""
    best = None
    best_area = -1
    for t in thumbs or []:
        if not isinstance(t, dict):
            continue
        url = t.get("url")
        if not url:
            continue
        area = int(t.get("width") or 0) * int(t.get("height") or 0)
        if area > best_area:
            best = url
            best_area = area
    return best


# --- models ----------------------------------------------------------------


@dataclass
class Video:
    video_id: str
    title: str
    channel: Optional[str] = None
    views: Optional[str] = None       # "3.4K views" — raw, includes the word "views"
    age: Optional[str] = None         # "1 month ago"
    duration: Optional[str] = None    # "5:28:00" or "12:34"
    badges: list[str] = field(default_factory=list)  # ["4K", "CC", ...]
    thumbnail_url: Optional[str] = None
    playlist_id: Optional[str] = None  # for autoplay-after-video queue
    params: Optional[str] = None       # watchEndpoint params (signed nav token)


@dataclass
class Shelf:
    title: str
    videos: list[Video] = field(default_factory=list)


@dataclass
class Feed:
    shelves: list[Shelf] = field(default_factory=list)
    continuation: Optional[str] = None  # for paging the home feed

    def all_videos(self) -> list[Video]:
        return [v for sh in self.shelves for v in sh.videos]


# --- parsers ---------------------------------------------------------------


def parse_tile(tile: dict) -> Optional[Video]:
    """Parse one tileRenderer into a Video. Returns None if it isn't a video tile."""
    on_select = tile.get("onSelectCommand", {})
    watch = on_select.get("watchEndpoint")
    if not watch or "videoId" not in watch:
        # not a video tile (could be channel/playlist tile) — skip for now
        return None

    header = tile.get("header", {}).get("tileHeaderRenderer", {})
    metadata = tile.get("metadata", {}).get("tileMetadataRenderer", {})

    thumb_url = _best_thumbnail(header.get("thumbnail", {}).get("thumbnails", []))

    duration = None
    for ov in header.get("thumbnailOverlays", []) or []:
        ts = ov.get("thumbnailOverlayTimeStatusRenderer")
        if ts:
            duration = _text(ts.get("text"))
            break

    title = _text(metadata.get("title")) or ""

    # Lines: typically line[0] = channel, line[1] = badges + views + age
    channel = None
    views = None
    age = None
    badges: list[str] = []
    for line in metadata.get("lines", []) or []:
        line_items = line.get("lineRenderer", {}).get("items", []) or []
        for item in line_items:
            li = item.get("lineItemRenderer", {})
            if "badge" in li:
                b = li["badge"].get("metadataBadgeRenderer", {})
                lbl = b.get("label")
                if lbl:
                    badges.append(lbl)
                continue
            txt = _text(li.get("text"))
            if not txt:
                continue
            low = txt.lower()
            # Multilingual heuristics — YT localises these strings
            # based on the hl/gl context (we send hl=ru so Russian
            # videos return Russian text). Keep English markers too
            # so mixed-language users still parse correctly.
            is_views = "view" in low or "просмотр" in low
            is_age = ("ago" in low
                      or "назад" in low
                      or low.startswith("стрим"))   # "Стрим был ... назад"
            if channel is None and not is_views and not is_age and txt != "•":
                channel = txt
            elif is_views:
                views = txt
            elif is_age:
                age = txt

    return Video(
        video_id=watch["videoId"],
        title=title,
        channel=channel,
        views=views,
        age=age,
        duration=duration,
        badges=badges,
        thumbnail_url=thumb_url,
        playlist_id=watch.get("playlistId"),
        params=watch.get("params"),
    )


def parse_video_renderer(node: dict) -> Optional[Video]:
    """Parse WEB/TV search item renderers (not only tileRenderer)."""
    vr = (
        node.get("videoRenderer")
        or node.get("gridVideoRenderer")
        or node.get("compactVideoRenderer")
        or node.get("playlistVideoRenderer")
    )
    if not vr:
        return None

    video_id = vr.get("videoId")
    if not video_id:
        nav = vr.get("navigationEndpoint") or vr.get("command", {})
        if isinstance(nav, dict):
            watch = nav.get("watchEndpoint") or {}
            video_id = watch.get("videoId")
    if not video_id or len(video_id) != 11:
        return None

    title = _text(vr.get("title")) or _text(vr.get("headline")) or ""
    thumb_url = _best_thumbnail(
        (vr.get("thumbnail") or {}).get("thumbnails", [])
    )

    channel = None
    owner = vr.get("ownerText") or vr.get("longBylineText") or vr.get("shortBylineText")
    if owner:
        channel = _text(owner)

    views = _text(vr.get("viewCountText")) or _text(vr.get("shortViewCountText"))
    age = _text(vr.get("publishedTimeText"))

    duration = None
    for key in ("lengthText", "thumbnailOverlays"):
        if key == "lengthText":
            duration = _text(vr.get("lengthText"))
            if duration:
                break
        for ov in vr.get("thumbnailOverlays", []) or []:
            ts = ov.get("thumbnailOverlayTimeStatusRenderer")
            if ts:
                duration = _text(ts.get("text"))
                break
        if duration:
            break

    return Video(
        video_id=video_id,
        title=title,
        channel=channel,
        views=views,
        age=age,
        duration=duration,
        thumbnail_url=thumb_url,
    )


def _videos_from_list_items(items: list) -> list[Video]:
    """Extract videos from a horizontal list / item section."""
    out: list[Video] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        tile = it.get("tileRenderer")
        if tile:
            v = parse_tile(tile)
            if v:
                out.append(v)
                continue
        v = parse_video_renderer(it)
        if v:
            out.append(v)
    return out


def parse_shelf(shelf_node: dict) -> Optional[Shelf]:
    """Parse one shelfRenderer into a Shelf with its videos."""
    sh = shelf_node.get("shelfRenderer")
    if not sh:
        return None
    header = sh.get("headerRenderer", {}).get("shelfHeaderRenderer", {})
    # TVHTML5 wraps the title inside an avatarLockup; older surfaces put it
    # directly on the header. Try both.
    title = (
        _text(header.get("title"))
        or _text(header.get("avatarLockup", {})
                       .get("avatarLockupRenderer", {})
                       .get("title"))
        or "<untitled>"
    )

    content = sh.get("content", {})
    items = content.get("horizontalListRenderer", {}).get("items", [])
    if not items:
        items = content.get("gridShelfViewModel", {}).get("contents", [])
    videos = _videos_from_list_items(items)
    return Shelf(title=title, videos=videos)


def _walk_sections(sections: list) -> Feed:
    """Common section-list walker — works for both home and /next pivot."""
    feed = Feed()
    for sec in sections:
        if "shelfRenderer" in sec:
            sh = parse_shelf(sec)
            if sh:
                feed.shelves.append(sh)
        elif "itemSectionRenderer" in sec:
            contents = sec["itemSectionRenderer"].get("contents", [])
            vids = _videos_from_list_items(contents)
            if vids:
                feed.shelves.append(Shelf(title="Результаты", videos=vids))
        elif "continuationItemRenderer" in sec:
            cont = (
                sec["continuationItemRenderer"]
                   .get("continuationEndpoint", {})
                   .get("continuationCommand", {})
                   .get("token")
            )
            if cont:
                feed.continuation = cont
    return feed


def parse_home(raw: dict) -> Feed:
    """Parse the FEwhat_to_watch response into a Feed."""
    try:
        sections = (
            raw["contents"]["tvBrowseRenderer"]["content"]
               ["tvSurfaceContentRenderer"]["content"]
               ["sectionListRenderer"]["contents"]
        )
    except (KeyError, TypeError):
        return Feed()
    return _walk_sections(sections)


def _search_section_lists(raw: dict) -> list[list]:
    """Collect section-list ``contents`` arrays from search JSON."""
    found: list[list] = []

    def add(sections: object) -> None:
        if isinstance(sections, list) and sections:
            found.append(sections)

    try:
        add(raw["contents"]["sectionListRenderer"]["contents"])
    except (KeyError, TypeError):
        pass
    try:
        add(raw["contents"]["tvSearchRenderer"]["content"]
            ["sectionListRenderer"]["contents"])
    except (KeyError, TypeError):
        pass
    try:
        add(raw["contents"]["twoColumnSearchResultsRenderer"]
            ["primaryContents"]["sectionListRenderer"]["contents"])
    except (KeyError, TypeError):
        pass
    try:
        add(raw["contents"]["tvBrowseRenderer"]["content"]
            ["tvSurfaceContentRenderer"]["content"]
            ["sectionListRenderer"]["contents"])
    except (KeyError, TypeError):
        pass
    return found


def parse_search(raw: dict) -> Feed:
    """Parse InnerTube /search (TV or WEB-shaped JSON)."""
    feed = Feed()
    seen: set[str] = set()

    for sections in _search_section_lists(raw):
        part = _walk_sections(sections)
        feed.continuation = feed.continuation or part.continuation
        for sh in part.shelves:
            uniq: list[Video] = []
            for v in sh.videos:
                if v.video_id in seen:
                    continue
                seen.add(v.video_id)
                uniq.append(v)
            if uniq:
                feed.shelves.append(Shelf(title=sh.title, videos=uniq))

    # Fallback: walk renderers (WEB search often nests videoRenderer deeply).
    if not feed.shelves:
        extras: list[Video] = []

        def walk(obj: object, depth: int = 0) -> None:
            if depth > 22 or len(extras) > 80:
                return
            if isinstance(obj, dict):
                if any(k in obj for k in (
                    "videoRenderer", "gridVideoRenderer",
                    "compactVideoRenderer", "tileRenderer",
                )):
                    v = parse_video_renderer(obj)
                    if not v and "tileRenderer" in obj:
                        v = parse_tile(obj["tileRenderer"])
                    if v and v.video_id not in seen:
                        seen.add(v.video_id)
                        extras.append(v)
                for v in obj.values():
                    walk(v, depth + 1)
            elif isinstance(obj, list):
                for x in obj[:60]:
                    walk(x, depth + 1)

        walk(raw.get("contents", raw))
        if extras:
            feed.shelves.append(Shelf(title="Результаты", videos=extras))

    return feed


def parse_browse_continuation(raw: dict) -> Feed:
    """Parse a browse continuation page (home/search section list).

    Continuation responses may omit the ``tvBrowseRenderer`` wrapper and
    expose ``sectionListRenderer`` directly — try both shapes.
    """
    for path in (
        lambda r: r["contents"]["tvBrowseRenderer"]["content"]
        ["tvSurfaceContentRenderer"]["content"]["sectionListRenderer"]["contents"],
        lambda r: r["contents"]["sectionListRenderer"]["contents"],
        lambda r: r["onResponseReceivedActions"][0]["appendContinuationItemsAction"]
        ["continuationItems"],
    ):
        try:
            sections = path(raw)
            if sections:
                return _walk_sections(sections)
        except (KeyError, TypeError, IndexError):
            continue
    return Feed()


def flatten_shelves_interleaved(feed: Feed) -> tuple[list[Video], list[str]]:
    """Round-robin across shelves — closer to YouTube's mixed home rows."""
    if not feed.shelves:
        return [], []
    max_len = max(len(sh.videos) for sh in feed.shelves)
    videos: list[Video] = []
    shelf_of: list[str] = []
    for i in range(max_len):
        for sh in feed.shelves:
            if i < len(sh.videos):
                videos.append(sh.videos[i])
                shelf_of.append(sh.title)
    return videos, shelf_of


def merge_feeds_for_grid(feeds: list[Feed], *, interleave: bool = True
                         ) -> tuple[list[Video], list[str]]:
    """Merge several feeds; dedupe by video_id, keep first shelf label."""
    seen: set[str] = set()
    videos: list[Video] = []
    shelf_of: list[str] = []
    for feed in feeds:
        if interleave:
            chunk_v, chunk_s = flatten_shelves_interleaved(feed)
        else:
            chunk_v, chunk_s = [], []
            for sh in feed.shelves:
                for v in sh.videos:
                    chunk_v.append(v)
                    chunk_s.append(sh.title)
        for v, label in zip(chunk_v, chunk_s):
            if v.video_id in seen:
                continue
            seen.add(v.video_id)
            videos.append(v)
            shelf_of.append(label)
    return videos, shelf_of


def pivot_seed_ids(home: Feed, *, max_seeds: int = 4) -> list[str]:
    """Pick diverse seeds for ``/next`` pivot — one per shelf, then random fill."""
    seeds: list[str] = []
    seen: set[str] = set()
    for sh in home.shelves:
        if not sh.videos:
            continue
        vid = sh.videos[0].video_id
        if vid not in seen:
            seeds.append(vid)
            seen.add(vid)
        if len(seeds) >= max_seeds:
            return seeds
    rest = [
        v.video_id
        for sh in home.shelves
        for v in sh.videos
        if v.video_id not in seen
    ]
    random.shuffle(rest)
    for vid in rest:
        seeds.append(vid)
        if len(seeds) >= max_seeds:
            break
    return seeds


def parse_next_pivot(raw: dict) -> Feed:
    """Parse `/next` response into a Feed using the `pivot` recommendations.

    TVHTML5 puts ~10 shelves of related content under
    contents.singleColumnWatchNextResults.pivot.sectionListRenderer —
    these are the up-next / related videos, much richer than home.
    """
    try:
        sections = (
            raw["contents"]["singleColumnWatchNextResults"]
               ["pivot"]["sectionListRenderer"]["contents"]
        )
    except (KeyError, TypeError):
        return Feed()
    return _walk_sections(sections)
