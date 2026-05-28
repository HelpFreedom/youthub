#!/usr/bin/env python3.11
"""TUI grid of YouTube thumbnails — first visual MVP.

Run in a kitty terminal:
  .venv/bin/python grid_demo.py

What happens:
  1. Pulls (or reuses cached) home feed via youthub.innertube
  2. Pre-fetches thumbnails for the first ~12 videos
  3. Draws them in a 4-column grid, fits to terminal
  4. Focus rectangle around the current tile
  5. Arrow keys / hjkl to navigate, Enter to print videoId, q to quit
"""
from __future__ import annotations

import colorsys
import json
import math
import os
import random
import signal
import subprocess
import sys
import termios
import threading
import time
import traceback
from pathlib import Path
from typing import Optional

from youthub import feed as feed_mod
from youthub import graphics, innertube, preview, terminal, thumbnails
from youthub.feed_loader import FeedLoader

HOME_CACHE = Path(__file__).resolve().parent / "cache" / "innertube_home.json"
NEXT_CACHE = Path(__file__).resolve().parent / "cache" / "innertube_next.json"
HOME_TTL_SECS = 120  # short — we want fresh stuff on reload
ERROR_LOG = Path(__file__).resolve().parent / "cache" / "grid_debug.log"
KEY_LOG = Path(__file__).resolve().parent / "cache" / "grid_keys.log"
SESSION_LOG = Path(__file__).resolve().parent / "cache" / "session.log"

# Краткое сообщение в строке подсказок (обновление, поиск, …).
_status_banner: str = ""
_status_banner_until: float = 0.0

# Time the focused tile must be unchanged before the hover preview starts.
HOVER_DELAY = 3.0

# Lock around any sequence of writes to stdout. Both the main thread
# (drawing tiles/status/etc.) and the preview worker (transmitting
# replacement frames) acquire it so escape sequences never interleave.
_screen_lock = threading.Lock()

# Key bindings — duplicated for the EN and RU keyboard layouts so the
# user doesn't have to switch layout to drive the grid. The Russian
# letters are the ones that sit physically on the same QWERTY keys
# (q→й, r→к, f→а).
QUIT_KEYS   = {"q", "Q", "й", "Й"}
RELOAD_KEYS = {"r", "R", "к", "К"}
SEARCH_KEYS = {"f", "F", "а", "А"}

# Подсказки в нижней строке (см. draw_status). От короткой к полной.
_STATUS_HOTKEYS = (
    "hjkl · Enter · f · r · PgUp/Dn · Home · q",
    "hjkl · Enter · f поиск · r обновить · PgUp/Dn · Home · q выход",
    "←↑↓→/hjkl · Enter воспр. · f поиск · r обновить · "
    "PgUp/PgDn листать · Home · q выход",
)
# Нижняя «хром»-полоса: подсказки (тёмная) + заголовок (инверсия, как было).
_STATUS_HINT_STYLE = "\033[48;5;236;38;5;250m"   # тёмно-серый фон, светлый текст
_STATUS_TITLE_STYLE = "\033[7m"                  # инвертированный (белая полоса)

# Удержание стрелок может давать десятки событий в секунду.
# Схлопываем короткий burst в один redraw, чтобы UI не "рвало".
_NAV_BURST_WINDOW_SEC = 0.012
_NAV_BURST_MAX_KEYS = 64


_PLAY_LOG = Path(__file__).resolve().parent / "cache" / "play.log"


# The old _BootstrapWarmer was removed when we switched to the
# bgutils + curl_cffi pipeline — there is no more Camoufox cold start
# to hide. The bridge does its own PR fetch in ~1.4 s, so prewarming
# saved nothing and (worse) the warmer thread would silently spin up a
# headless Camoufox in the background, hang on the proxy, and starve
# the main loop. If we ever want prewarming again it should call the
# bridge directly via a dry START_SESSION + STOP_SESSION pair.


def _xdo_terminal_geometry(env: dict) -> tuple[int, int, int, int] | None:
    """Return (x, y, w, h) of the active X11 window (the terminal),
    captured right before we launch ffplay so we know where to drop
    its window. None if xdotool can't see anything."""
    try:
        wid = subprocess.run(
            ["xdotool", "getactivewindow"],
            capture_output=True, text=True, env=env, timeout=2,
        ).stdout.strip()
        if not wid:
            return None
        out = subprocess.run(
            ["xdotool", "getwindowgeometry", "--shell", wid],
            capture_output=True, text=True, env=env, timeout=2,
        ).stdout
        vals: dict[str, int] = {}
        for line in out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                try:
                    vals[k] = int(v)
                except ValueError:
                    pass
        if all(k in vals for k in ("X", "Y", "WIDTH", "HEIGHT")):
            return vals["X"], vals["Y"], vals["WIDTH"], vals["HEIGHT"]
    except Exception:
        pass
    return None


def _xdo_find_ffplay(title: str, env: dict) -> str | None:
    """Try to find ffplay window by title and by WM_CLASS=ffplay."""
    for args in (["--name", title], ["--class", "ffplay"]):
        try:
            r = subprocess.run(
                ["xdotool", "search", *args],
                capture_output=True, text=True, env=env, timeout=2,
            )
            wid = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
            if wid:
                return wid
        except Exception:
            pass
    return None


def _xdo_place_ffplay(title: str, env: dict, timeout: float = 6.0,
                      on_found=None) -> None:
    """Poll for ffplay window, then aggressively focus it.

    On tiling WMs (user has dwm) we can't really overlay onto the
    terminal tile — the WM puts ffplay in its own tile. What we CAN
    fix is the keyboard focus, so the user's `q` actually closes the
    player instead of going to the wrong tile.

    `on_found` is invoked once as soon as the window first appears —
    used by the grid to stop the "connecting" spinner.
    """
    deadline = time.time() + timeout
    wid: str | None = None
    while time.time() < deadline:
        wid = _xdo_find_ffplay(title, env)
        if wid:
            break
        time.sleep(0.1)
    if not wid:
        if on_found is not None:
            try:
                on_found()
            except Exception:
                pass
        return
    if on_found is not None:
        try:
            on_found()
        except Exception:
            pass
    # Multiple focus attempts — different WMs honor different mechanisms.
    # We repeat each because some WMs only commit focus after a redraw.
    for _ in range(3):
        for cmd in (
            ["xdotool", "windowactivate", "--sync", wid],
            ["xdotool", "windowfocus", "--sync", wid],
            ["xdotool", "windowraise", wid],
            ["wmctrl", "-i", "-a", wid],
        ):
            try:
                subprocess.run(cmd, env=env, timeout=2,
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
            except Exception:
                pass
        time.sleep(0.15)


def play_video(video_id: str, on_ready=None) -> int:
    """Live SABR stream into ffplay (no on-disk muxed file).

    Pipeline: sabr_bridge.mjs (curl_cffi /watch + bgutils PO Token) →
    ffmpeg mux → ffplay-yt over IPC. The window sits on top of the
    kitty grid; when the user closes it, the whole chain tears down.

    `on_ready`, if given, is called once as soon as the ffplay window
    first appears on screen (or the wait times out) — used by the
    grid to dismiss the loading spinner.

    All chatter goes to cache/play.log so the grid stays clean.
    """
    # Late import — bridge_player pulls in subprocess + socket plumbing.
    import bridge_player as bp_mod  # type: ignore[import-not-found]

    _PLAY_LOG.parent.mkdir(parents=True, exist_ok=True)
    title = f"YouTube — {video_id}"

    # Capture terminal geometry BEFORE we lose focus to ffplay.
    # `env` is what we hand to the bridge subprocess — must include
    # HTTPS_PROXY so the bundled curl_cffi /watch fetch and SABR
    # requests go through the user's proxy. We just propagate the
    # parent process environment as-is.
    env = dict(os.environ)
    env.setdefault("DISPLAY", ":0")
    term_geom = _xdo_terminal_geometry(env)

    w = h = None
    if term_geom:
        _x, _y, w, h = term_geom

    current_vid = video_id
    while True:
        player = None
        try:
            try:
                player = bp_mod.start_player(
                    current_vid,
                    window_title=f"YouTube — {current_vid}",
                    window_w=w,
                    window_h=h,
                    log_path=_PLAY_LOG,
                    env=env,
                )
            except Exception as e:
                with open(_PLAY_LOG, "a") as f:
                    f.write(f"\n[grid] bridge_player.start failed: {e}\n")
                return 1

            # Focus the new ffplay window so the user's `q` actually
            # closes it (dwm sometimes parks floating windows behind).
            # Only fire the on_ready callback on the FIRST iteration —
            # on switch-video reloads the spinner is already gone.
            cb = on_ready if current_vid == video_id else None
            placer = threading.Thread(
                target=_xdo_place_ffplay,
                args=(f"YouTube — {current_vid}", env),
                kwargs={"on_found": cb},
                daemon=True,
            )
            placer.start()

            rc = player.wait()
            graphics.reset_terminal_modes()
            # If the user picked a tile in the sidebar, the bridge
            # caught a PLAY_VIDEO event and stashed the new id before
            # exiting. Loop with that id; otherwise return rc.
            nxt = getattr(player, "next_video_id", None)
            if not nxt:
                return rc
            current_vid = nxt
        finally:
            if player is not None:
                player.kill()


def _read_fresh(path: Path, ttl: int) -> dict | None:
    if path.exists() and (time.time() - path.stat().st_mtime) < ttl:
        return json.loads(path.read_text())
    return None


def _fetch_home_enriched(it: innertube.InnerTube,
                         *, max_continuation_pages: int = 0) -> feed_mod.Feed:
    """Home feed, optionally plus continuation pages (more shelves)."""
    home = feed_mod.parse_home(it.home())
    shelves = list(home.shelves)
    cont = home.continuation
    pages = 0
    while cont and pages < max_continuation_pages:
        more = feed_mod.parse_browse_continuation(
            it.browse("FEwhat_to_watch", continuation=cont))
        shelves.extend(more.shelves)
        cont = more.continuation
        pages += 1
    return feed_mod.Feed(shelves=shelves, continuation=cont)


def _fetch_pivot_merged(it: innertube.InnerTube,
                        seed_ids: list[str]) -> feed_mod.Feed:
    """Merge ``/next`` pivot shelves from several seed videos."""
    merged_shelves: list[feed_mod.Shelf] = []
    for seed in seed_ids:
        try:
            pivot = feed_mod.parse_next_pivot(it.next(seed))
            merged_shelves.extend(pivot.shelves)
        except Exception as e:
            print(f"[grid] pivot {seed} failed: {e}", file=sys.stderr)
    return feed_mod.Feed(shelves=merged_shelves)


def _log_session(msg: str) -> None:
    SESSION_LOG.parent.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n"
    with SESSION_LOG.open("a", encoding="utf-8") as f:
        f.write(line)
    print(f"[grid] {msg}", file=sys.stderr)


def set_status_banner(msg: str, *, secs: float = 8.0) -> None:
    global _status_banner, _status_banner_until
    _status_banner = msg
    _status_banner_until = time.time() + secs


def load_feed_combined(
    *,
    refresh: bool = False,
    exclude_ids: set[str] | None = None,
) -> tuple[list[feed_mod.Video], list[str]]:
    """Fetch home + pivot recommendations, merge for the grid.

    Normal load: cache-friendly, one random pivot seed, home block then pivot.
    Refresh (``r``): no cache, home + up to 2 continuation pages, several
    pivot seeds, shelves interleaved like YouTube's mixed rows.
    """
    if refresh:
        with innertube.InnerTube() as it:
            home = _fetch_home_enriched(it, max_continuation_pages=2)
            if home.shelves:
                random.shuffle(home.shelves)
            seeds = feed_mod.pivot_seed_ids(home, max_seeds=6)
            pivot = _fetch_pivot_merged(it, seeds) if seeds else feed_mod.Feed()
            videos, shelf_of = feed_mod.merge_feeds_for_grid(
                [home, pivot], interleave=True)
        if exclude_ids:
            pairs = [(v, s) for v, s in zip(videos, shelf_of)
                     if v.video_id not in exclude_ids]
            random.shuffle(pairs)
            if len(pairs) < 12:
                back = [(v, s) for v, s in zip(videos, shelf_of)
                        if v.video_id in exclude_ids]
                random.shuffle(back)
                need = min(len(back), 12 - len(pairs))
                pairs.extend(back[:need])
            if pairs:
                videos, shelf_of = map(list, zip(*pairs))
            else:
                videos, shelf_of = [], []
        else:
            pairs = list(zip(videos, shelf_of))
            random.shuffle(pairs)
            if pairs:
                videos, shelf_of = map(list, zip(*pairs))
        _log_session(
            f"refresh: {len(home.shelves)} home shelves, "
            f"{len(seeds)} pivot seeds → {len(videos)} videos"
            + (f" (без {len(exclude_ids)} старых)" if exclude_ids else ""))
        return videos, shelf_of

    raw_home = _read_fresh(HOME_CACHE, HOME_TTL_SECS)
    if raw_home is None:
        with innertube.InnerTube() as it:
            raw_home = it.home()
        HOME_CACHE.write_text(json.dumps(raw_home, ensure_ascii=False))
    home = feed_mod.parse_home(raw_home)

    all_home_videos = [v for sh in home.shelves for v in sh.videos]
    seed_id: str | None = None
    if all_home_videos:
        seed_id = random.choice(all_home_videos).video_id

    pivot = feed_mod.Feed()
    if seed_id:
        raw_next = _read_fresh(NEXT_CACHE, HOME_TTL_SECS)
        if raw_next is None or _seed_of(raw_next) != seed_id:
            with innertube.InnerTube() as it:
                raw_next = it.next(seed_id)
            NEXT_CACHE.write_text(json.dumps(raw_next, ensure_ascii=False))
        pivot = feed_mod.parse_next_pivot(raw_next)

    return feed_mod.merge_feeds_for_grid([home, pivot], interleave=False)


def _seed_of(raw_next: dict) -> str | None:
    """Pull the seed videoId out of a /next response so we can tell if
    the cache was for the same seed video."""
    try:
        return raw_next.get("currentVideoEndpoint", {})\
            .get("watchEndpoint", {}).get("videoId")
    except (AttributeError, TypeError):
        return None


# --- layout ---------------------------------------------------------------


def compute_layout(ts: terminal.TermSize, target_tile_w: int = 32):
    """Compute grid that fills the screen with tiles ~target_tile_w cells wide.

    Aspect 16:9 in pixels; tile_h derived from kitty's reported cell px size
    (falls back to a cell ratio of 1:2 if unknown). Vertical leftover (the
    rows that don't fit a full tile) is distributed back into the inter-row
    gaps so the grid actually fills the screen instead of leaving a void
    at the bottom.
    """
    cell_w = ts.cell_w or 9
    cell_h = ts.cell_h or 18
    gutter = 2
    top_margin = 2
    bottom_margin = 2
    side_margin = 2

    usable_cols = ts.cols - 2 * side_margin
    n_cols = max(1, (usable_cols + gutter) // (target_tile_w + gutter))
    tile_w = (usable_cols - gutter * (n_cols - 1)) // n_cols

    tile_h_px = tile_w * cell_w * 9 / 16
    tile_h = max(3, int(tile_h_px / cell_h))
    text_rows = 2
    min_row_gap = 1   # плотнее вертикально: на средних экранах чаще влезает 3 ряда
    row_content = tile_h + text_rows

    rows_available = ts.rows - top_margin - bottom_margin
    # n_rows such that n_rows*row_content + (n_rows-1)*min_row_gap fits
    n_rows = max(
        1,
        (rows_available + min_row_gap) // (row_content + min_row_gap),
    )
    # Не размазываем leftover в межрядные зазоры: большие "дырки" между рядами
    # визуально ломают сетку и оставляют место для артефактов рамки.
    row_gap = min_row_gap
    row_total = row_content + row_gap

    return {
        "tile_w": tile_w,
        "tile_h": tile_h,
        "text_rows": text_rows,
        "gutter": gutter,
        "row_gap": row_gap,
        "row_total": row_total,
        "top_margin": top_margin,
        "left_margin": side_margin,
        "n_cols": n_cols,
        "n_rows": n_rows,
    }


def tile_origin(layout: dict, idx: int) -> tuple[int, int]:
    """1-indexed (row, col) terminal cell for the top-left of tile #idx."""
    n_cols = layout["n_cols"]
    r = idx // n_cols
    c = idx % n_cols
    row = layout["top_margin"] + 1 + r * layout["row_total"]
    col = layout["left_margin"] + 1 + c * (layout["tile_w"] + layout["gutter"])
    return row, col


# --- drawing --------------------------------------------------------------

W = sys.stdout.buffer


def write(b: bytes) -> None:
    W.write(b)


def writes(s: str) -> None:
    W.write(s.encode())


def clamp(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[: max(0, n - 1)] + "…"


def draw_tile(layout: dict, idx: int, video: feed_mod.Video, focused: bool) -> None:
    row, col = tile_origin(layout, idx)
    tile_w = layout["tile_w"]
    tile_h = layout["tile_h"]

    # Clear the two text rows first — prevents leftover chars when the
    # previous tile in this slot had a longer title.
    for r in (row + tile_h, row + tile_h + 1):
        graphics.move_cursor(r, col)
        writes(" " * tile_w)

    graphics.move_cursor(row, col)
    png = thumbnails.get_png(video.video_id)
    if png is not None:
        graphics.transmit_and_place(
            image_id=idx + 1,
            png=png,
            width_cells=tile_w,
            height_cells=tile_h,
        )

    # Duration badge (bottom-right of thumbnail)
    if video.duration:
        dur_text = clamp(video.duration, tile_w - 2)
        graphics.move_cursor(row + tile_h - 1, col + tile_w - len(dur_text) - 1)
        writes(f"\033[40;97m {dur_text} \033[0m")

    # Title — focused: bold bright yellow; otherwise muted white
    title = clamp(video.title, tile_w)
    graphics.move_cursor(row + tile_h, col)
    if focused:
        writes(f"\033[1;93m{title}\033[0m")
    else:
        writes(f"\033[37m{title}\033[0m")
    # Channel + views
    meta = video.channel or ""
    if video.views:
        meta = f"{meta} · {video.views}" if meta else video.views
    meta = clamp(meta, tile_w)
    graphics.move_cursor(row + tile_h + 1, col)
    if focused:
        writes(f"\033[93m{meta}\033[0m")
    else:
        writes(f"\033[90m{meta}\033[0m")

    if focused:
        draw_focus_border(row, col, tile_w, tile_h)
    else:
        # Defensive: erase any focus border lingering from a previous
        # render (e.g. after the viewport scrolled and this slot's
        # video changed).
        clear_focus_border(row, col, tile_w, tile_h)


# Heavy box-drawing chars for the focus border. These are 1 cell each
# and combine into a clean rectangle around the thumbnail+meta block.
_BORDER = {
    "tl": "┏", "tr": "┓", "bl": "┗", "br": "┛",
    "h":  "━", "v":  "┃",
}


def draw_focus_border(row: int, col: int, w: int, h: int) -> None:
    """Heavy yellow rectangle around the focused tile (thumb + 2 text rows)."""
    color = "\033[1;93m"  # bright bold yellow
    reset = "\033[0m"
    text_rows = 2
    top = row - 1
    bottom = row + h + text_rows
    left = col - 1
    right = col + w
    # top line
    graphics.move_cursor(top, left)
    writes(color + _BORDER["tl"] + _BORDER["h"] * w + _BORDER["tr"] + reset)
    # bottom line
    graphics.move_cursor(bottom, left)
    writes(color + _BORDER["bl"] + _BORDER["h"] * w + _BORDER["br"] + reset)
    # left and right verticals across thumbnail + 2 text rows
    for r in range(row, row + h + text_rows):
        graphics.move_cursor(r, left)
        writes(color + _BORDER["v"] + reset)
        graphics.move_cursor(r, right)
        writes(color + _BORDER["v"] + reset)


def clear_focus_border(row: int, col: int, w: int, h: int) -> None:
    text_rows = 2
    top = row - 1
    bottom = row + h + text_rows
    left = col - 1
    right = col + w
    # top + bottom
    graphics.move_cursor(top, left)
    writes(" " * (w + 2))
    graphics.move_cursor(bottom, left)
    writes(" " * (w + 2))
    # verticals
    for r in range(row, row + h + text_rows):
        graphics.move_cursor(r, left)
        writes(" ")
        graphics.move_cursor(r, right)
        writes(" ")


def clear_slot(layout: dict, slot_idx: int) -> None:
    """Wipe a grid slot: delete its image and blank the two text lines."""
    graphics.delete_image(slot_idx + 1)
    row, col = tile_origin(layout, slot_idx)
    w = layout["tile_w"]
    # Two text rows under the image
    for r in (row + layout["tile_h"], row + layout["tile_h"] + 1):
        graphics.move_cursor(r, col)
        writes(" " * w)
    # Focus brackets if any
    clear_focus_border(row, col, w, layout["tile_h"])


def redraw_grid(layout: dict, ts: terminal.TermSize, videos, shelf_of, offset: int,
                focus: int) -> None:
    """Redraw every visible slot for the current offset & focus."""
    # Полная очистка рабочей области (всё между шапкой и статусом).
    # Это добивает любые хвосты по краям экрана после быстрых переходов.
    for r in range(2, max(2, ts.rows - 1)):
        graphics.move_cursor(r, 1)
        writes(" " * ts.cols)

    cap = layout["n_cols"] * layout["n_rows"]
    for slot in range(cap):
        global_idx = offset + slot
        if global_idx < len(videos):
            draw_tile(layout, slot, videos[global_idx],
                      focused=(global_idx == focus))
        else:
            clear_slot(layout, slot)


def draw_header(layout: dict, ts: terminal.TermSize,
                header_title: str, idx: int, total: int) -> None:
    """Top chrome row. Keep it stable while moving focus."""
    graphics.move_cursor(1, 1)
    writes("\033[2K")
    graphics.move_cursor(1, 2)
    writes("\033[1;96m" + clamp(f"YouTube — {header_title}", 70) + "\033[0m")
    graphics.move_cursor(1, max(2, ts.cols - 14))
    writes(f"\033[90m[{idx + 1}/{total}]\033[0m")


def _status_hotkey_hint(cols: int) -> str:
    """Строка подсказок, влезающая в ширину терминала."""
    for hint in reversed(_STATUS_HOTKEYS):
        if len(hint) <= max(cols - 2, 0):
            return hint
    return _STATUS_HOTKEYS[0][: max(cols - 2, 0)]


def _fill_status_line(row: int, cols: int, text: str, style: str) -> None:
    """Одна строка статуса на всю ширину терминала с заливкой фона."""
    graphics.move_cursor(row, 1)
    writes("\033[2K")
    bar = text.ljust(cols)[:cols]
    writes(style)
    writes(bar)
    writes("\033[0m")


def draw_status(ts: terminal.TermSize, video: feed_mod.Video, last_key: str = "") -> None:
    """Нижние 2 строки: подсказки (тёмная полоса) + заголовок (инверсия)."""
    global _status_banner, _status_banner_until
    hint = _status_hotkey_hint(max(ts.cols - 2, 0))
    if _status_banner and time.time() < _status_banner_until:
        banner = clamp(_status_banner, max(ts.cols // 2, 20))
        hint = f"{banner} · {hint}"
    elif _status_banner:
        _status_banner = ""
    if ts.rows >= 2:
        _fill_status_line(ts.rows - 1, ts.cols, hint, _STATUS_HINT_STYLE)

    tail = f" [{last_key}]" if last_key else ""
    space = ts.cols - len(tail)
    if space < 8:
        space = 8
    head = clamp(f" {video.title}", space)
    title_bar = f"{head}{tail}"
    _fill_status_line(ts.rows, ts.cols, title_bar, _STATUS_TITLE_STYLE)


# --- connecting animation -------------------------------------------------

# Braille spinner — ten frames, smooth at ~70ms per tick. Each glyph
# is one terminal cell so we don't have to deal with widths.
_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# Block heights for the audio-visualiser bars. 8 levels of vertical
# fill — perfect for HSV-coloured equalizer rows.
_BARS = "▁▂▃▄▅▆▇█"


def _rainbow_rgb(i: int, n: int, phase: float = 0.0) -> tuple[int, int, int]:
    """Map a position+phase to a saturated rainbow RGB.

    Hue cycles around the colour wheel; lightness fixed at 0.55 keeps
    everything readably bright on a dark terminal background.
    """
    h = ((i / max(1, n)) + phase * 0.03) % 1.0
    r, g, b = colorsys.hls_to_rgb(h, 0.55, 0.95)
    return int(r * 255), int(g * 255), int(b * 255)


def _truecolor(r: int, g: int, b: int) -> str:
    return f"\033[38;2;{r};{g};{b}m"


class ConnectingAnimation:
    """Centered animated panel shown while the bridge spins up ffplay.

    Layout (12 rows):
        ╭── СЕЙЧАС ЗАГРУЖАЕТСЯ ────────╮
        │                              │
        │  ▄▆█▇▅▃▂▁▂▄▆█▇…  (4 rows of │
        │  ▂▄▇█▆▄▁▂▃▅▇█▆…   equaliser) │
        │  ▃▅▇█▆▄▂▁▂▄▆█▇…              │
        │  ▁▃▅█▇▅▃▁▂▄▆█▇…              │
        │                              │
        │       ⠹  ▶ Запускаем плеер…  │
        │                              │
        │   ═──═══─═─────══─═─── (sep) │
        │                              │
        │           Video title         │
        ╰──────────────────────────────╯

    Border + equaliser + separator all shift colour with the phase,
    so the whole panel breathes. Spinner + dots animate in step.
    """

    def __init__(self, ts: terminal.TermSize, title: str):
        self._ts = ts
        self._title = title
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        with _screen_lock:
            graphics.delete_all()
            graphics.clear_screen()
            W.flush()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.5)
            self._thread = None

    def _run(self) -> None:
        ts = self._ts
        # Adapt to terminal size, but stay readable.
        box_w = max(46, min(74, ts.cols - 6))
        box_h = max(11, min(13, ts.rows - 4))
        box_r = max(2, (ts.rows - box_h) // 2)
        box_c = max(2, (ts.cols - box_w) // 2)
        bar_w = box_w - 6
        title = clamp(self._title, box_w - 8)

        phase = 0
        while not self._stop.is_set():
            self._draw(phase, box_r, box_c, box_w, box_h, bar_w, title)
            phase += 1
            if self._stop.wait(0.07):  # ~14 fps
                break

    def _draw(self, phase: int, br: int, bc: int, bw: int, bh: int,
              bar_w: int, title: str) -> None:
        # --- border (rainbow that drifts with phase) ----------------
        bcr, bcg, bcb = _rainbow_rgb(phase, 80, phase * 0.5)
        border = _truecolor(bcr, bcg, bcb)
        reset = "\033[0m"
        label = " СЕЙЧАС ЗАГРУЖАЕТСЯ "
        with _screen_lock:
            graphics.move_cursor(br, bc)
            writes(border + "╭" + label
                   + "─" * max(0, bw - 2 - len(label)) + "╮" + reset)
            for r in range(br + 1, br + bh - 1):
                graphics.move_cursor(r, bc)
                writes(border + "│" + reset
                       + " " * (bw - 2)
                       + border + "│" + reset)
            graphics.move_cursor(br + bh - 1, bc)
            writes(border + "╰" + "─" * (bw - 2) + "╯" + reset)

            # --- equaliser bars (4 rows of HSL-coloured cells) ------
            n_rows = min(4, bh - 7)
            for row_off in range(n_rows):
                row = br + 2 + row_off
                graphics.move_cursor(row, bc + 3)
                line = []
                for i in range(bar_w):
                    v = (
                        math.sin(phase * 0.18 + i * 0.50 + row_off * 0.40) +
                        math.sin(phase * 0.32 + i * 0.30 - row_off * 0.70) +
                        math.sin(phase * 0.50 + i * 0.70)
                    ) / 3.0
                    v = (v + 1.0) * 0.5  # 0..1
                    idx = max(0, min(7, int(v * 8)))
                    cr, cg, cb = _rainbow_rgb(i + phase,
                                              bar_w * 2,
                                              row_off * 0.2)
                    line.append(_truecolor(cr, cg, cb) + _BARS[idx])
                writes("".join(line) + reset)

            # --- spinner + status line ------------------------------
            spinner = _SPINNER_FRAMES[phase % len(_SPINNER_FRAMES)]
            dots = "." * ((phase // 3) % 4)
            msg = f"{spinner}  ▶  Запускаем плеер{dots}"
            msg_row = br + bh - 5
            graphics.move_cursor(msg_row, bc + 1)
            writes(" " * (bw - 2))
            graphics.move_cursor(msg_row,
                                 bc + max(1, (bw - len(msg)) // 2))
            writes(f"\033[1;93m{msg}\033[0m")

            # --- shimmering separator -------------------------------
            sep_row = br + bh - 3
            graphics.move_cursor(sep_row, bc + 3)
            seg = []
            for i in range(bar_w):
                d = abs(((phase + i) % 14) - 7) / 7.0
                if d < 0.35:
                    cr, cg, cb = _rainbow_rgb(i + phase, bar_w)
                    seg.append(_truecolor(cr, cg, cb) + "═")
                else:
                    seg.append("\033[90m─")
            writes("".join(seg) + reset)

            # --- title (static, muted) ------------------------------
            t_row = br + bh - 2
            graphics.move_cursor(t_row, bc + 1)
            writes(" " * (bw - 2))
            graphics.move_cursor(t_row,
                                 bc + max(1, (bw - len(title)) // 2))
            writes(f"\033[37m{title}\033[0m")

            W.flush()


# --- search overlay -------------------------------------------------------


class SearchOverlay:
    """Centered text-entry box overlaid on top of the tile grid.

    Doesn't clear the tiles underneath — kitty images keep showing
    through the gaps around the box, which makes opening/closing feel
    instantaneous. We just draw a bordered rectangle of spaces, the
    label, the typed query, and a hint line.
    """

    BORDER_FG = "\033[1;96m"   # bright cyan
    LABEL_FG  = "\033[1;97m"   # bright white
    TEXT_FG   = "\033[97m"
    HINT_FG   = "\033[90m"
    RESET     = "\033[0m"

    def __init__(self, ts: terminal.TermSize):
        self.open = False
        self.query = ""
        self._ts = ts
        self._box = self._compute_box(ts)

    @staticmethod
    def _compute_box(ts: terminal.TermSize) -> tuple[int, int, int, int]:
        """Return (row, col, width, height) for the centred box."""
        width = max(30, min(64, ts.cols - 4))
        height = 5
        row = max(2, (ts.rows - height) // 2)
        col = max(2, (ts.cols - width) // 2)
        return row, col, width, height

    def relayout(self, ts: terminal.TermSize) -> None:
        self._ts = ts
        self._box = self._compute_box(ts)

    def toggle(self) -> bool:
        self.open = not self.open
        if not self.open:
            self.query = ""
        return self.open

    def close(self) -> None:
        self.open = False
        self.query = ""

    def overlapping_slots(self, layout: dict, cap: int) -> list[int]:
        """Return slot indices whose tile rectangle intersects this box.

        We pad the tile rect by 1 cell on every side to also catch the
        focus border, which otherwise pokes out from behind the box.
        """
        box_row, box_col, bw, bh = self._box
        b_top, b_bot = box_row, box_row + bh - 1
        b_left, b_right = box_col, box_col + bw - 1
        out: list[int] = []
        for slot in range(cap):
            tr, tc = tile_origin(layout, slot)
            tw, th = layout["tile_w"], layout["tile_h"]
            # Tile span: image + 2 text rows below, focus border ±1.
            t_top = tr - 1
            t_bot = tr + th + 2
            t_left = tc - 1
            t_right = tc + tw
            if (t_bot < b_top or t_top > b_bot or
                    t_right < b_left or t_left > b_right):
                continue
            out.append(slot)
        return out

    def feed_char(self, ch: str) -> None:
        # Only accept printable single-codepoint characters; reject
        # control bytes and our key tokens like "up" / "tab".
        if len(ch) >= 1 and ch[0].isprintable() and not ch.startswith("\x1b"):
            # Cap length so the input never overflows the visible field.
            row, col, w, h = self._box
            max_chars = w - 6
            if len(self.query) < max_chars:
                self.query += ch

    def backspace(self) -> None:
        self.query = self.query[:-1]

    def draw(self) -> None:
        row, col, w, h = self._box
        with _screen_lock:
            # Top border
            graphics.move_cursor(row, col)
            label = " Поиск YouTube "
            top = ("┌" + label + "─" * max(0, w - 2 - len(label)) + "┐")
            writes(self.BORDER_FG + top + self.RESET)
            # Middle rows (blanked)
            for r in range(row + 1, row + h - 1):
                graphics.move_cursor(r, col)
                writes(self.BORDER_FG + "│" + self.RESET
                       + " " * (w - 2)
                       + self.BORDER_FG + "│" + self.RESET)
            # Bottom border with hint
            graphics.move_cursor(row + h - 1, col)
            hint = " Enter — поиск · Esc — отмена "
            if len(hint) > w - 2:
                hint = hint[:w - 2]
            bot = ("└" + hint + "─" * max(0, w - 2 - len(hint)) + "┘")
            writes(self.BORDER_FG + bot + self.RESET)
            # Input line: "> <query>_"
            inp_row = row + 2
            inp_col = col + 2
            field_w = w - 4
            graphics.move_cursor(inp_row, inp_col)
            # Slide the visible window so the cursor is always in view.
            visible = self.query
            if len(visible) > field_w - 3:
                visible = visible[-(field_w - 3):]
            line = f"{self.LABEL_FG}▎{self.RESET} {self.TEXT_FG}{visible}\033[7m \033[0m"
            writes(line)
            W.flush()


# --- search execution -----------------------------------------------------


def run_search(query: str) -> tuple[list[feed_mod.Video], list[str]]:
    """InnerTube search (TV, then WEB context if TV returned nothing)."""
    with innertube.InnerTube() as it:
        raw = it.search(query)
        parsed = feed_mod.parse_search(raw)
        n = sum(len(sh.videos) for sh in parsed.shelves)
        if n == 0:
            raw = it.search_web(query)
            parsed = feed_mod.parse_search(raw)
            n = sum(len(sh.videos) for sh in parsed.shelves)
            _log_session(f"search «{query}»: WEB fallback → {n} videos")
        else:
            _log_session(f"search «{query}»: TV → {n} videos")
    videos: list[feed_mod.Video] = []
    shelf_of: list[str] = []
    label = f"Поиск: {query}"
    seen: set[str] = set()
    for sh in parsed.shelves:
        for v in sh.videos:
            if v.video_id in seen:
                continue
            seen.add(v.video_id)
            videos.append(v)
            shelf_of.append(sh.title.strip() or label)
    return videos, shelf_of


# --- reload helper --------------------------------------------------------


def reload_feed(
    exclude_ids: set[str] | None = None,
) -> tuple[list[feed_mod.Video], list[str]]:
    """Fresh fetch for the r/к reload key — YouTube-like mixed refresh."""
    for p in (HOME_CACHE, NEXT_CACHE):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
    return load_feed_combined(refresh=True, exclude_ids=exclude_ids)


# --- main loop ------------------------------------------------------------


def scroll_offset_for(focus: int, offset: int, layout: dict) -> int:
    """Return an offset (always row-aligned) that keeps `focus` visible."""
    n_cols = layout["n_cols"]
    cap = n_cols * layout["n_rows"]
    if focus < offset:
        return (focus // n_cols) * n_cols
    if focus >= offset + cap:
        bottom_row = focus // n_cols
        return max(0, (bottom_row - layout["n_rows"] + 1) * n_cols)
    return offset


def clamp_focus(focus: int, n: int) -> int:
    return max(0, min(focus, n - 1))


def _apply_nav_key(cur_focus: int, key: str, *, n_cols: int, cap: int,
                   total: int) -> int | None:
    """Return updated focus for one nav key; None if key isn't navigation."""
    if key in (terminal.KEY_LEFT, "h"):
        return max(0, cur_focus - 1)
    if key in (terminal.KEY_RIGHT, "l"):
        return min(total - 1, cur_focus + 1)
    if key in (terminal.KEY_UP, "k"):
        return max(0, cur_focus - n_cols)
    if key in (terminal.KEY_DOWN, "j"):
        return min(total - 1, cur_focus + n_cols)
    if key == terminal.KEY_PGUP:
        return max(0, cur_focus - cap)
    if key == terminal.KEY_PGDN:
        return min(total - 1, cur_focus + cap)
    if key == terminal.KEY_HOME:
        return 0
    return None


def main() -> int:
    print("[grid] loading home + watch-next pivot…", file=sys.stderr)
    initial_videos, initial_shelf_of = load_feed_combined()
    if not initial_videos:
        print("[grid] no videos parsed", file=sys.stderr)
        return 1

    loader = FeedLoader(initial_videos, initial_shelf_of)
    videos, shelf_of = loader.snapshot()

    print(f"[grid] {len(videos)} videos loaded, feed will extend on scroll",
          file=sys.stderr)
    print("[grid] fetching thumbnails…", file=sys.stderr)
    thumbnails.prefetch([v.video_id for v in videos[:60]])

    # --- terminal resize plumbing ------------------------------------------
    # SIGWINCH fires on resize. We just set a flag — the main loop reflows
    # next iteration. Doing real work in the handler is unsafe (Python
    # signals run between bytecodes, can interrupt mid-write).
    resize_pending = threading.Event()
    resize_pending.set()  # set once to trigger the initial layout pass

    def on_winch(signum, frame):
        resize_pending.set()

    prev_winch = signal.signal(signal.SIGWINCH, on_winch)

    # --- initial state -----------------------------------------------------
    ts = terminal.size()
    layout = compute_layout(ts)
    focus = 0
    offset = 0
    header_title = "Рекомендованные"
    last_key = ""
    crash_info: str | None = None
    focus_changed_at = time.time()
    preview_player = preview.PreviewPlayer()
    search_overlay = SearchOverlay(ts)

    def full_redraw():
        nonlocal videos, shelf_of
        videos, shelf_of = loader.snapshot()
        with _screen_lock:
            graphics.delete_all()
            graphics.clear_screen()
            if videos:
                draw_header(layout, ts, header_title,
                            focus, len(videos))
                redraw_grid(layout, ts, videos, shelf_of, offset, focus)
                draw_status(ts, videos[focus], last_key=last_key)
            W.flush()

    def make_preview_cb(video_id: str, slot_idx: int,
                        cap_layout: dict):
        """Build a frame callback bound to the snapshotted slot+layout.

        The closure captures the slot+layout that were valid when the
        preview was started. If state has since changed (focus moved,
        terminal resized), the worker can't know — so we accept that
        one stale frame may render before our `.stop()` lands; the
        next redraw of that slot will overwrite it.
        """
        row, col = tile_origin(cap_layout, slot_idx)
        w, h = cap_layout["tile_w"], cap_layout["tile_h"]
        image_id = slot_idx + 1

        def cb(png: bytes) -> None:
            with _screen_lock:
                graphics.move_cursor(row, col)
                graphics.transmit_and_place(
                    image_id=image_id, png=png,
                    width_cells=w, height_cells=h,
                )
                W.flush()
        return cb

    KEY_LOG.parent.mkdir(parents=True, exist_ok=True)
    key_log = KEY_LOG.open("w")
    queued_key: str | None = None
    try:
        with terminal.KeyReader() as keys:
            while True:
                # Resize → stop any preview (its layout snapshot is stale)
                # and do a fresh full redraw.
                if resize_pending.is_set():
                    resize_pending.clear()
                    preview_player.stop()
                    ts = terminal.size()
                    layout = compute_layout(ts)
                    focus = clamp_focus(focus, len(videos))
                    offset = scroll_offset_for(focus, offset, layout)
                    full_redraw()
                    search_overlay.relayout(ts)
                    cap = layout["n_cols"] * layout["n_rows"]
                    if search_overlay.open:
                        for slot in search_overlay.overlapping_slots(
                                layout, cap):
                            clear_slot(layout, slot)
                        search_overlay.draw()
                    thumbnails.prefetch(
                        [v.video_id for v in videos[offset:offset + cap * 2]]
                    )
                    focus_changed_at = time.time()

                # Top-up the feed when we're 2 rows from the loaded end.
                # FeedLoader's worker prefetches thumbnails internally, so
                # we don't need a second prefetch here.
                if len(videos) - focus < 2 * layout["n_cols"]:
                    loader.maybe_extend()
                    new_videos, new_shelf_of = loader.snapshot()
                    if len(new_videos) != len(videos):
                        videos, shelf_of = new_videos, new_shelf_of

                if videos and not search_overlay.open:
                    cur_video_id = videos[focus].video_id
                    elapsed = time.time() - focus_changed_at
                    # Hover preview after focus settles.
                    if elapsed >= HOVER_DELAY:
                        if not preview_player.is_playing_for(cur_video_id):
                            preview_player.start(
                                cur_video_id,
                                make_preview_cb(cur_video_id,
                                                focus - offset, layout),
                            )

                if queued_key is not None:
                    k = queued_key
                    queued_key = None
                else:
                    k = keys.read(timeout=0.2)
                if k is None:
                    continue
                last_key = k
                key_log.write(f"{time.time():.3f}  k={k!r}\n")
                key_log.flush()

                # --- search overlay key path ---------------------------
                # When the box is open, swallow keys for the input.
                # Only Esc closes — f/а must stay typeable inside the
                # query (otherwise words like "fox" or "автомобиль"
                # would dismiss the box mid-typing).
                if search_overlay.open:
                    if k == terminal.KEY_ESC:
                        search_overlay.close()
                        full_redraw()
                        focus_changed_at = time.time()
                        continue
                    if k == terminal.KEY_ENTER:
                        query = search_overlay.query.strip()
                        if not query:
                            continue
                        search_overlay.close()
                        # Show a "searching…" notice in the status bar
                        # so the user sees something during the round-trip.
                        with _screen_lock:
                            graphics.move_cursor(ts.rows, 1)
                            writes("\033[2K\033[1;96m"
                                   f"  Ищем «{clamp(query, 40)}»…\033[0m")
                            W.flush()
                        try:
                            new_v, new_s = run_search(query)
                        except Exception as e:
                            with _screen_lock:
                                graphics.move_cursor(ts.rows, 1)
                                writes("\033[2K\033[91m"
                                       f"  Ошибка поиска: {e}\033[0m")
                                W.flush()
                            continue
                        if not new_v:
                            set_status_banner(
                                f"По запросу «{query}» ничего не найдено")
                            full_redraw()
                            continue
                        loader.replace(new_v, new_s)
                        set_status_banner(
                            f"Найдено: {len(new_v)} · «{clamp(query, 30)}»")
                        header_title = f"Поиск: {clamp(query, 32)}"
                        videos, shelf_of = loader.snapshot()
                        focus = 0
                        offset = 0
                        thumbnails.prefetch(
                            [v.video_id for v in videos[:60]])
                        full_redraw()
                        focus_changed_at = time.time()
                        continue
                    if k == terminal.KEY_BACKSPACE:
                        search_overlay.backspace()
                        search_overlay.draw()
                        continue
                    # Plain printable character → into the query.
                    # Reject special key tokens (multi-char names like
                    # "up", "tab", "pgdn") by checking length AND that
                    # the value is a real codepoint string.
                    if (len(k) == 1 and k.isprintable()) or (
                            len(k) == 2 and k.isprintable()):
                        search_overlay.feed_char(k)
                        search_overlay.draw()
                    continue

                # --- normal grid key path ------------------------------
                if k in QUIT_KEYS:
                    break

                if k in SEARCH_KEYS:
                    preview_player.stop()
                    search_overlay.toggle()
                    if search_overlay.open:
                        # kitty draws images on top of text by default,
                        # so the box would be hidden beneath the tile
                        # thumbnails. Delete the images that overlap
                        # the box first; full_redraw on close brings
                        # them back.
                        cap = layout["n_cols"] * layout["n_rows"]
                        for slot in search_overlay.overlapping_slots(
                                layout, cap):
                            clear_slot(layout, slot)
                        search_overlay.draw()
                    else:
                        full_redraw()
                        focus_changed_at = time.time()
                    continue

                if k in RELOAD_KEYS:
                    preview_player.stop()
                    loader.pause()
                    with _screen_lock:
                        graphics.move_cursor(ts.rows, 1)
                        writes("\033[2K\033[1;96m"
                               "  Обновляем ленту…\033[0m")
                        W.flush()
                    try:
                        old_ids = {v.video_id for v in videos}
                        new_v, new_s = reload_feed(exclude_ids=old_ids)
                    except Exception as e:
                        with _screen_lock:
                            graphics.move_cursor(ts.rows, 1)
                            writes("\033[2K\033[91m"
                                   f"  Ошибка обновления: {e}\033[0m")
                            W.flush()
                        loader.resume()
                        continue
                    loader.resume()
                    if new_v:
                        loader.replace(new_v, new_s)
                        header_title = "Рекомендованные"
                        videos, shelf_of = loader.snapshot()
                        focus = 0
                        offset = 0
                        thumbnails.prefetch(
                            [v.video_id for v in videos[:60]])
                        set_status_banner(
                            f"Обновлено: {len(new_v)} видео · лог: cache/session.log")
                    else:
                        set_status_banner("Не удалось обновить ленту")
                    full_redraw()
                    focus_changed_at = time.time()
                    continue

                if k == terminal.KEY_ENTER:
                    chosen_video = videos[focus]
                    preview_player.stop()
                    loader.pause()

                    # Loading animation between Enter and the ffplay
                    # window appearing. The xdotool placer fires
                    # on_ready as soon as the window is up.
                    anim = ConnectingAnimation(ts, chosen_video.title)
                    anim.start()

                    # Recommendations for the chosen video now live in
                    # the ffplay-yt sidebar (Tab), so the grid stays on
                    # the home feed while the player is up.
                    keys.suspend()
                    rc = 0
                    try:
                        rc = play_video(chosen_video.video_id,
                                        on_ready=anim.stop)
                    finally:
                        anim.stop()
                        keys.resume()
                        loader.resume()
                        try:
                            termios.tcflush(sys.stdin.fileno(),
                                            termios.TCIFLUSH)
                        except Exception:
                            pass

                    if rc != 0:
                        sys.stderr.write(
                            f"\n[grid] play_video failed (exit {rc}). "
                            f"See {_PLAY_LOG} for details. Press any key.\n")
                        sys.stderr.flush()
                        keys.suspend()
                        try:
                            sys.stdin.read(1)
                        except Exception:
                            pass
                        keys.resume()
                    # Force a clean redraw of the (restored) home feed.
                    resize_pending.set()
                    focus_changed_at = time.time()
                    continue

                n_cols = layout["n_cols"]
                cap = n_cols * layout["n_rows"]
                new_focus = _apply_nav_key(
                    focus, k, n_cols=n_cols, cap=cap, total=len(videos))
                if new_focus is None:
                    with _screen_lock:
                        draw_status(ts, videos[focus], last_key=last_key)
                        W.flush()
                    continue

                # Collapse buffered key-repeat burst into one final focus.
                burst_deadline = time.time() + _NAV_BURST_WINDOW_SEC
                burst_count = 0
                while burst_count < _NAV_BURST_MAX_KEYS and time.time() < burst_deadline:
                    k2 = keys.read(timeout=0.0)
                    if k2 is None:
                        break
                    key_log.write(f"{time.time():.3f}  k={k2!r}\n")
                    key_log.flush()
                    nxt = _apply_nav_key(
                        new_focus, k2, n_cols=n_cols, cap=cap, total=len(videos))
                    if nxt is None:
                        queued_key = k2
                        break
                    last_key = k2
                    new_focus = nxt
                    burst_count += 1

                if new_focus == focus:
                    with _screen_lock:
                        draw_status(ts, videos[focus], last_key=last_key)
                        W.flush()
                    continue

                # Focus is moving — kill any preview before mutating
                # state, then redraw. (preview_player.stop() doesn't
                # need the lock; main holds nothing here.)
                preview_player.stop()

                new_offset = scroll_offset_for(new_focus, offset, layout)
                offset = new_offset
                focus = new_focus
                # Надёжнее частичных дельта-апдейтов: перерисовываем весь
                # видимый viewport. Чуть дороже, зато без "жёлтых хвостов"
                # рамки при быстрых переходах вверх/вниз/влево/вправо.
                with _screen_lock:
                    draw_header(layout, ts, header_title, focus, len(videos))
                    redraw_grid(layout, ts, videos, shelf_of, offset, focus)
                    draw_status(ts, videos[focus], last_key=last_key)
                    W.flush()
                focus_changed_at = time.time()
    except Exception:
        crash_info = traceback.format_exc()
    finally:
        key_log.close()
        signal.signal(signal.SIGWINCH, prev_winch)
        preview_player.stop()
        loader.stop()

    # Cleanup: delete all images, clear screen, reset any terminal
    # modes mpv/other subprocesses might have left enabled (mouse
    # tracking is the common culprit — manifests as garbage characters
    # on cursor movement in the parent shell).
    graphics.delete_all()
    graphics.clear_screen()
    graphics.reset_terminal_modes()
    graphics.move_cursor(1, 1)
    W.flush()

    if crash_info:
        ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
        ERROR_LOG.write_text(crash_info)
        print("[grid] CRASHED — traceback written to "
              f"{ERROR_LOG}", file=sys.stderr)
        print(crash_info, file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
