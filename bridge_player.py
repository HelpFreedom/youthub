#!/usr/bin/env python3.11
"""Live SABR → ffplay-yt pipeline with fast seek-anywhere.

Architecture:

    bootstrap.py  (Camoufox/Chromium)
            │  one-time per video: capture real PO Token POST + cache
            │  ytInitialPlayerResponse so the bridge skips a /watch refetch
            ▼
    sabr_bridge.mjs  --control /tmp/<id>.sock      (PERSISTENT)
            │  loaded once: playerResponse, youtubei.js decipher, PO Token,
            │  formats, SabrStream class. Listens on a unix socket for:
            │     START_SESSION path=<file> start_at=<sec>
            │     STOP_SESSION   /   QUIT
            │  Each START_SESSION aborts the previous SabrStream + ffmpeg
            │  child, spawns a fresh one writing muxed matroska to <file>.
            ▼
    /tmp/ytlive_<id>_<n>.mkv   (one file per session)
            ▼
    ffplay-yt   -i <file>   -ipc /tmp/<id>.ipc.sock
            │  Arrow keys route through IPC to bridge_player. Small
            │  in-file targets → SEEK_REL inside ffplay. Targets past
            │  the file → START_SESSION on the bridge for a new file,
            │  then OPEN <new file> on ffplay-yt (no window flicker).

Why persistent bridge: each seek-anywhere used to kill the Node bridge
and start fresh, paying ~3 s of Node startup + module imports + youtubei
decipher + /watch fetch on every seek. Persistent cuts that to ~1 s —
the bare minimum needed for one SABR roundtrip + matroska header bytes.
"""
from __future__ import annotations

import os
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

PROJECT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_DIR))

import re

import sponsorblock as sb_mod  # noqa: E402
from watchstats import WatchStats  # noqa: E402


FFPLAY_YT = PROJECT_DIR / "ffplay-yt" / "bin" / "ffplay-yt"


def _video_id_from_url(url: str) -> str:
    """Extract YouTube video ID (same rules as bootstrap.video_id_from_url)."""
    m = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", url)
    if m:
        return m.group(1)
    m = re.search(r"youtu\.be/([A-Za-z0-9_-]{11})", url)
    if m:
        return m.group(1)
    m = re.search(r"/(?:embed|shorts)/([A-Za-z0-9_-]{11})", url)
    if m:
        return m.group(1)
    raise ValueError(f"could not extract video id from URL: {url!r}")

# 2 MB ≈ a couple of seconds of 1080p — buffer between current playhead
# and "must restart" decision so we don't trigger restarts on jitter.
SEEK_SAFETY_BYTES = 2 * 1024 * 1024

# Minimum time between accepted seek requests. The previous seek's
# new POS needs to reach us before we make another decision, otherwise
# spam-pressing arrows would target stale positions and trigger
# pointless restart cascades.
SEEK_COOLDOWN_SEC = 0.5

# Recommendation sidebar (Tab inside ffplay-yt). On by default — set
# SIDEBAR_RECS=0 to disable if it ever causes regressions.
SIDEBAR_RECS = os.environ.get("SIDEBAR_RECS", "1") == "1"
RECS_PIPELINE = PROJECT_DIR / "recs_pipeline.py"


# ---------------------------------------------------------------------------
# dwm focus helper (same as before)
# ---------------------------------------------------------------------------

def _xdo_find_ffplay(title: str, env: dict) -> Optional[str]:
    for args in (["--name", title], ["--class", "ffplay-yt"], ["--class", "ffplay"]):
        try:
            r = subprocess.run(
                ["xdotool", "search", *args],
                capture_output=True, text=True, env=env, timeout=2,
            )
            wid = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
            if wid: return wid
        except Exception:
            pass
    return None


def _xdo_focus_player(title: str, env: dict, timeout: float = 6.0) -> None:
    deadline = time.time() + timeout
    wid: Optional[str] = None
    while time.time() < deadline:
        wid = _xdo_find_ffplay(title, env)
        if wid: break
        time.sleep(0.1)
    if not wid: return
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


# ---------------------------------------------------------------------------
# Bridge control protocol
# ---------------------------------------------------------------------------

def _wait_for_path(path: Path, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return False


def _wait_for_min_bytes(path: Path, n: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if path.stat().st_size >= n:
                return True
        except FileNotFoundError:
            pass
        time.sleep(0.05)
    return False


class BridgeControl:
    """Persistent Unix-socket connection to sabr_bridge.mjs.

    Each START_SESSION / STOP_SESSION / QUIT command returns the
    bridge's reply line so the caller knows whether the new session
    started successfully.
    """

    def __init__(self, sock_path: Path):
        self._path = sock_path
        self._sock: Optional[socket.socket] = None
        self._lock = threading.Lock()

    def connect(self, timeout: float = 10.0) -> bool:
        if not _wait_for_path(self._path, timeout):
            return False
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect(str(self._path))
            self._sock = s
            return True
        except Exception:
            return False

    def send(self, line: str, *, reply_timeout: float = 30.0) -> Optional[str]:
        s = self._sock
        if s is None:
            return None
        with self._lock:
            try:
                s.sendall((line.rstrip("\n") + "\n").encode())
                # Read one reply line. May be ERR or OK ...
                old_to = s.gettimeout()
                s.settimeout(reply_timeout)
                buf = b""
                while b"\n" not in buf:
                    chunk = s.recv(1024)
                    if not chunk: break
                    buf += chunk
                s.settimeout(old_to)
                return buf.split(b"\n", 1)[0].decode("utf-8", "ignore")
            except Exception:
                return None

    def close(self) -> None:
        if self._sock is not None:
            try: self._sock.close()
            except Exception: pass
            self._sock = None


# ---------------------------------------------------------------------------
# LivePlayer
# ---------------------------------------------------------------------------

@dataclass
class LivePlayer:
    bridge: subprocess.Popen
    bridge_ctrl: BridgeControl
    ffplay: subprocess.Popen
    tmpfile: Path
    ipc_socket: Path
    bridge_socket: Path
    log_path: Path
    video_id: str
    window_title: str
    env: dict
    _last_pos_sec: float = 0.0
    _file_start_sec: float = 0.0
    _last_seek_at: float = 0.0
    # SponsorBlock segments: (start_sec, end_sec, category) sorted by start.
    # Auto-skip triggered when playhead enters a segment.
    sponsor_segments: list = field(default_factory=list)
    _last_skipped_segment: Optional[tuple] = None
    _restart_lock: threading.Lock = field(default_factory=threading.Lock)
    _shutdown: threading.Event = field(default_factory=threading.Event)
    _ctrl_sock: Optional[socket.socket] = None
    _ctrl_thread: Optional[threading.Thread] = None
    _session_counter: int = 0
    # Set when the user picks a tile in the sidebar (PLAY_VIDEO event).
    # grid_demo reads it after wait() returns and restarts play_video.
    next_video_id: Optional[str] = None
    # Continuation state for the infinite-scroll recommendations.
    # `_recs_continuation` is the token for the next page (None when
    # exhausted). `_recs_loading` debounces overlapping LOAD_MORE_RECS
    # requests so we don't fire multiple fetches concurrently.
    _recs_continuation: Optional[str] = None
    _recs_loading: bool = False
    _recs_lock: threading.Lock = field(default_factory=threading.Lock)
    # Pings YouTube /api/stats/watchtime so the algorithm sees real
    # watch activity. Started after the IPC controller is up (we need
    # the POS feed for current-time updates), stopped in kill().
    _watchstats: Optional[WatchStats] = None

    # ---- lifecycle ----

    def start_controller(self) -> None:
        self._ctrl_thread = threading.Thread(
            target=self._controller_loop, daemon=True,
            name="bridge_player.controller")
        self._ctrl_thread.start()

    def wait(self) -> int:
        rc = self.ffplay.wait()
        self.kill()
        return rc

    def kill(self) -> None:
        self._shutdown.set()
        # Final watchtime ping (state=ended) before tearing down — best
        # effort, capped at 2s inside WatchStats.stop().
        if self._watchstats is not None:
            try: self._watchstats.stop()
            except Exception: pass
        # Tell bridge to QUIT cleanly first (so it can release SABR
        # sessions, kill its ffmpeg child, unlink its socket).
        try: self.bridge_ctrl.send("QUIT", reply_timeout=1.0)
        except Exception: pass
        self.bridge_ctrl.close()
        if self._ctrl_sock is not None:
            try: self._ctrl_sock.close()
            except Exception: pass
        for p in (self.ffplay, self.bridge):
            if p and p.poll() is None:
                try: p.terminate()
                except Exception: pass
        deadline = time.time() + 1.5
        for p in (self.ffplay, self.bridge):
            if not p: continue
            timeout = max(0.0, deadline - time.time())
            try: p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try: p.kill()
                except Exception: pass
        # Final guarantee: SIGKILL the bridge's process group. Node's
        # ffmpeg children + any zombie sub-procs get blown away in one
        # shot. Without this user reports lingering processes after q.
        if self.bridge is not None:
            try:
                os.killpg(os.getpgid(self.bridge.pid), 9)
            except (ProcessLookupError, PermissionError):
                pass
            except Exception:
                pass
        for p in (self.tmpfile, self.ipc_socket, self.bridge_socket):
            try: p.unlink(missing_ok=True)
            except Exception: pass

    # ---- IPC controller (ffplay-yt side) ----

    def _connect_ipc(self) -> Optional[socket.socket]:
        deadline = time.time() + 10.0
        while time.time() < deadline and not self._shutdown.is_set():
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(str(self.ipc_socket))
                s.settimeout(0.5)
                return s
            except (FileNotFoundError, ConnectionRefusedError):
                time.sleep(0.1)
            except Exception:
                time.sleep(0.1)
        return None

    def _send_ipc(self, line: str) -> bool:
        s = self._ctrl_sock
        if s is None: return False
        try:
            s.sendall((line.rstrip("\n") + "\n").encode())
            return True
        except Exception:
            return False

    def _controller_loop(self) -> None:
        log = open(self.log_path, "a")
        def cprint(m: str) -> None:
            try: log.write(m + "\n"); log.flush()
            except Exception: pass

        self._ctrl_sock = self._connect_ipc()
        if self._ctrl_sock is None:
            cprint("[controller] could not connect to ffplay-yt IPC")
            log.close()
            return
        cprint("[controller] connected")
        if SIDEBAR_RECS:
            self._start_sidebar_recs(cprint)
        self._watchstats = WatchStats(
            video_id=self.video_id,
            get_pos_fn=lambda: self._last_pos_sec,
            shutdown_event=self._shutdown,
            log_fn=cprint,
        )
        self._watchstats.start()

        buf = b""
        try:
            while not self._shutdown.is_set():
                try: chunk = self._ctrl_sock.recv(4096)
                except socket.timeout: continue
                except Exception: break
                if not chunk: break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    self._handle_event(line.decode("utf-8", "ignore").strip(), cprint)
        finally:
            cprint("[controller] exiting")
            log.close()

    def _handle_event(self, line: str, cprint) -> None:
        if not line: return
        if line.startswith("POS "):
            try:
                # ffplay's POS = master clock, which is the file's PTS.
                # bridge ffmpeg runs with -copyts so PTS is absolute
                # video time — POS reports are directly comparable to
                # `_file_start_sec` for seek decisions.
                self._last_pos_sec = float(line[4:])
            except ValueError: pass
            self._check_sponsor_skip(cprint)
            return
        if line.startswith("SEEK_REQ_REL "):
            try: delta = float(line[len("SEEK_REQ_REL "):])
            except ValueError: return
            self._handle_seek_req(delta, cprint)
            return
        if line.startswith("PLAY_VIDEO "):
            new_vid = line[len("PLAY_VIDEO "):].strip()
            if len(new_vid) != 11:
                cprint(f"[sidebar] ignoring malformed PLAY_VIDEO: {new_vid!r}")
                return
            cprint(f"[sidebar] PLAY_VIDEO {new_vid} — restarting player")
            self.next_video_id = new_vid
            # Ask ffplay-yt to quit gracefully. wait() will return,
            # grid_demo.play_video reads next_video_id and re-enters.
            self._send_ipc("QUIT")
            return
        if line == "LOAD_MORE_RECS":
            self._load_more_recs(cprint)
            return
        cprint(f"[controller] unknown event: {line!r}")

    # ---- sidebar recommendations ----

    def _run_recs_subprocess(self, args: list[str], label: str,
                             cprint) -> None:
        """Shared worker body: spawn recs_pipeline.py with `args`, pipe
        each stdout line into ffplay-yt's IPC, intercept CONTINUATION
        lines for the infinite-scroll state. Used for both first batch
        and continuation pages."""
        try:
            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=os.environ.copy(),
            )
        except Exception as e:
            cprint(f"[sidebar] failed to spawn {label}: {e}")
            return
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                if self._shutdown.is_set(): break
                line = raw.decode("utf-8", "ignore").rstrip("\n")
                if not line: continue
                if line.startswith("CONTINUATION "):
                    with self._recs_lock:
                        self._recs_continuation = line[len("CONTINUATION "):].strip()
                    continue
                self._send_ipc(line)
            rc = proc.wait(timeout=2)
            if rc != 0:
                err = (proc.stderr.read().decode("utf-8", "ignore")
                       if proc.stderr else "")
                cprint(f"[sidebar] {label} rc={rc}  {err.strip()}")
            else:
                cprint(f"[sidebar] {label} done")
        except Exception as e:
            cprint(f"[sidebar] {label} worker error: {e}")
            try: proc.kill()
            except Exception: pass

    def _start_sidebar_recs(self, cprint) -> None:
        """First batch — runs in a daemon thread so the controller loop
        isn't blocked while recommendations are fetched."""
        args = [sys.executable, str(RECS_PIPELINE), self.video_id]
        t = threading.Thread(
            target=self._run_recs_subprocess,
            args=(args, "recs", cprint),
            daemon=True, name="bridge_player.sidebar_recs")
        t.start()

    def _load_more_recs(self, cprint) -> None:
        """Triggered by LOAD_MORE_RECS from ffplay-yt when the user
        scrolls near the end of the list. Spawns recs_pipeline with the
        saved continuation token; debounced so overlapping requests fire
        only one fetch."""
        with self._recs_lock:
            if self._recs_loading:
                return
            token = self._recs_continuation
            if not token:
                return
            # Consume the token — _run_recs_subprocess will set a fresh
            # one if the new page also has continuation. If the fetch
            # fails, we just stop scrolling beyond this point.
            self._recs_continuation = None
            self._recs_loading = True

        def worker():
            try:
                self._run_recs_subprocess(
                    [sys.executable, str(RECS_PIPELINE),
                     "--continuation", self.video_id, token],
                    "more recs", cprint,
                )
            finally:
                with self._recs_lock:
                    self._recs_loading = False

        t = threading.Thread(target=worker, daemon=True,
                             name="bridge_player.sidebar_recs_more")
        t.start()

    # ---- SponsorBlock ----

    def _check_sponsor_skip(self, cprint) -> None:
        """Auto-seek past any sponsor segment the playhead landed in.

        Called on every POS event. We use the same `_handle_seek_req`
        machinery as user-triggered seeks, so:
          * a small skip becomes an in-file SEEK_REL — instant
          * a big skip (segment ending past our buffer) triggers a
            bridge restart at the segment's end — same logic as
            user pressing forward beyond the buffer
        Cooldown / restart-in-progress checks already handle the
        race where the controller fires multiple skip requests
        while one is pending.
        """
        if not self.sponsor_segments:
            return
        pos = self._last_pos_sec
        for seg in self.sponsor_segments:
            start, end, cat = seg
            # 0.3 s margin — don't trigger right at the boundary
            # where the next POS update would jump us out anyway.
            if pos < start + 0.1 or pos >= end - 0.3:
                continue
            if self._last_skipped_segment == seg:
                # Already triggered this segment; don't loop.
                return
            self._last_skipped_segment = seg
            cprint(f"[sponsor] skip {cat}  {start:.1f} → {end:.1f}  "
                   f"(pos={pos:.1f})")
            # Tell ffplay-yt to show its SponsorBlock overlay before
            # the actual seek fires — the overlay's accent colour
            # comes from the category, the caption from the duration.
            try:
                self._send_ipc(f"SPONSOR_SKIP {cat} {end - pos:.2f}")
            except Exception:
                pass
            self._handle_seek_req(end - pos, cprint)
            return
        # Outside all segments — clear the "just skipped" marker so
        # the next time we enter one (after a backward seek, say) we
        # skip again.
        if self._last_skipped_segment is not None:
            self._last_skipped_segment = None

    # ---- seek decision ----

    def _handle_seek_req(self, delta_sec: float, cprint) -> None:
        # Drop the request entirely if another seek is in flight.
        # `_restart_lock.locked()` catches the longer big-jump path;
        # the cooldown catches small in-file seeks plus an accidental
        # double-press right after a restart finishes.
        now = time.monotonic()
        if self._restart_lock.locked():
            cprint(f"[controller] SEEK_REQ_REL {delta_sec:+.1f}  "
                   f"IGNORED (restart in progress)")
            return
        if now - self._last_seek_at < SEEK_COOLDOWN_SEC:
            cprint(f"[controller] SEEK_REQ_REL {delta_sec:+.1f}  "
                   f"IGNORED (cooldown {SEEK_COOLDOWN_SEC*1000:.0f}ms)")
            return
        self._last_seek_at = now

        target = max(0.0, self._last_pos_sec + delta_sec)
        try: size = self.tmpfile.stat().st_size
        except FileNotFoundError: size = 0
        played_in_file = max(self._last_pos_sec - self._file_start_sec, 1.0)
        bytes_per_sec = (size / played_in_file) if size > 0 else 0
        target_offset_sec = target - self._file_start_sec
        target_bytes = target_offset_sec * bytes_per_sec if bytes_per_sec > 0 else 0
        in_range = (
            target >= self._file_start_sec
            and target_bytes > 0
            and target_bytes < size - SEEK_SAFETY_BYTES
        )
        cprint(
            f"[controller] SEEK_REQ_REL {delta_sec:+.1f}  "
            f"pos={self._last_pos_sec:.1f}  target={target:.1f}  "
            f"fileStart={self._file_start_sec:.1f}  "
            f"file={size/1024/1024:.1f}MB  bps={bytes_per_sec/1024:.0f}KB/s  "
            f"in_range={in_range}"
        )
        if in_range:
            self._send_ipc(f"SEEK_REL {delta_sec}")
        else:
            threading.Thread(
                target=self._restart_at, args=(target, cprint),
                daemon=True, name="bridge_player.restart",
            ).start()

    # ---- restart workflow ----

    def _restart_at(self, target_sec: float, cprint) -> None:
        if not self._restart_lock.acquire(blocking=False):
            cprint(f"[restart] skipped (already in progress) "
                   f"target={target_sec:.1f}s")
            return
        try:
            cprint(f"[restart] BEGIN -> {target_sec:.1f}s")
            t0 = time.time()
            self._session_counter += 1
            new_tmp = Path(tempfile.mkstemp(
                prefix=f"ytlive_{self.video_id}_s{self._session_counter}_",
                suffix=".mkv")[1])
            # Tell the bridge to swap sessions. It aborts current
            # SabrStream + ffmpeg internally and starts a new pair
            # writing to new_tmp at the requested timestamp.
            reply = self.bridge_ctrl.send(
                f"START_SESSION path={new_tmp} start_at={target_sec:.3f}",
                reply_timeout=30.0,
            )
            cprint(f"[restart]   bridge reply: {reply!r} ({time.time()-t0:.1f}s)")
            if not reply or not reply.startswith("OK"):
                cprint(f"[restart] bridge refused — giving up")
                try: new_tmp.unlink()
                except Exception: pass
                return

            # Prime the tmpfile with enough data that ffplay won't
            # immediately catch up to the writer's live edge. 512 KB
            # was ~3 seconds of 1080p content — fine for a single seek,
            # but back-to-back seeks made ffplay hit "File ended
            # prematurely" because the matroska demuxer ran out of
            # clusters while the bridge was still trickling them in
            # (especially when YT throttles SABR with the
            # `stream protection {status:1}` slowdown). 4 MB gives
            # ~20-30s of runway — bridge keeps writing in the background
            # so ffplay never sees a starving file again. Extra cost is
            # 2-5s of wait time on the seek that triggered a restart.
            if not _wait_for_min_bytes(new_tmp, 4 * 1024 * 1024, timeout=25.0):
                cprint(f"[restart] not enough bytes from new session "
                       f"({time.time()-t0:.1f}s) — giving up")
                try: new_tmp.unlink()
                except Exception: pass
                return
            cprint(f"[restart]   tmpfile primed ({time.time()-t0:.1f}s)")

            # Unlink the OLD tmpfile (ffplay still has its fd open;
            # the inode survives until ffplay reopens).
            try: self.tmpfile.unlink()
            except Exception: pass
            self.tmpfile = new_tmp
            self._last_pos_sec = target_sec
            self._file_start_sec = target_sec
            self._send_ipc(f"OPEN {new_tmp}")
            cprint(f"[restart] DONE OPEN {new_tmp} ({time.time()-t0:.1f}s)")
        finally:
            self._restart_lock.release()


# ---------------------------------------------------------------------------
# start_player
# ---------------------------------------------------------------------------

def start_player(
    video_id: str,
    *,
    window_title: str,
    window_w: Optional[int] = None,
    window_h: Optional[int] = None,
    extra_ffplay: Optional[list[str]] = None,
    log_path: Optional[Path] = None,
    env: Optional[dict[str, str]] = None,
    _force_rebootstrap: bool = False,
    _retry: int = 1,
) -> LivePlayer:
    env = dict(env or os.environ)
    log_path = log_path or (PROJECT_DIR / "cache" / "live_play.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Always append — overwriting on each launch erases diagnostic
    # logs of bugs that happened in the previous session. Sessions are
    # delimited by the "=== bridge_player <vid> ===" line so it's easy
    # to scan back. Log file is small per session, won't bloat in
    # practice.
    log = open(log_path, "a")

    def logprint(msg: str) -> None:
        log.write(msg + "\n"); log.flush()

    logprint(f"=== bridge_player {video_id} ===")

    # PO Token + playerResponse now come from inside the Node bridge
    # (bgutils-js + browser-headers /watch fetch). No Camoufox needed.
    # `_force_rebootstrap` is kept as a no-op param so the retry path
    # below can still call us with it; bgutils auto-refreshes its own
    # integrity token when stale.

    # 2) Spawn the persistent bridge with a control socket.
    bridge_sock = Path(tempfile.mkstemp(
        prefix=f"ytlive_{video_id}_bridge_", suffix=".sock")[1])
    bridge_sock.unlink(missing_ok=True)
    bridge_cmd = ["node", str(PROJECT_DIR / "sabr_bridge.mjs"),
                  video_id, "--control", str(bridge_sock)]
    logprint(f"bridge spawn: {shlex.join(bridge_cmd)}")
    # start_new_session=True puts the bridge (and its ffmpeg children)
    # in their own process group so we can SIGKILL the whole tree on
    # cleanup if the polite QUIT path is unresponsive.
    bridge = subprocess.Popen(bridge_cmd, cwd=PROJECT_DIR, env=env,
                              stdout=log, stderr=log,
                              stdin=subprocess.DEVNULL,
                              start_new_session=True)

    bridge_ctrl = BridgeControl(bridge_sock)
    if not bridge_ctrl.connect(timeout=20.0):
        logprint("bridge did not open control socket in 20s — aborting")
        try: bridge.terminate()
        except Exception: pass
        try: bridge.wait(timeout=2.0)
        except Exception: pass
        try: os.killpg(os.getpgid(bridge.pid), 9)
        except Exception: pass
        if _retry > 0:
            logprint("bridge did not come up — retrying with fresh bootstrap")
            print("[bridge_player] bridge did not come up — retrying with "
                  "fresh bootstrap", file=sys.stderr, flush=True)
            log.close()
            return start_player(
                video_id, window_title=window_title,
                window_w=window_w, window_h=window_h,
                extra_ffplay=extra_ffplay, log_path=log_path, env=env,
                _force_rebootstrap=True, _retry=_retry - 1,
            )
        raise RuntimeError("bridge failed to start")
    logprint("bridge control socket connected")

    # 3) Start the initial session.
    tmpfile = Path(tempfile.mkstemp(
        prefix=f"ytlive_{video_id}_s0_", suffix=".mkv")[1])
    # Generous reply_timeout: sabr_bridge.fetchWatchPlayerResponse now
    # walks the full pr_fetch rotation (24 TLS×IP strategies) before
    # giving up. Realistic worst case is ~72s (24 × ~3s bot-wall
    # rejection); we allow 180s to absorb the occasional slow proxy /
    # warm-up timing without prematurely killing the bridge mid-cycle.
    reply = bridge_ctrl.send(
        f"START_SESSION path={tmpfile} start_at=0", reply_timeout=180.0)
    logprint(f"START_SESSION reply: {reply!r}")
    if not reply or not reply.startswith("OK"):
        try: bridge.terminate()
        except Exception: pass
        try: os.killpg(os.getpgid(bridge.pid), 9)
        except Exception: pass
        if _retry > 0:
            logprint(f"START_SESSION refused ({reply!r}) — retrying with "
                     "fresh bootstrap")
            print("[bridge_player] bridge START_SESSION failed — retrying "
                  "with fresh bootstrap", file=sys.stderr, flush=True)
            log.close()
            return start_player(
                video_id, window_title=window_title,
                window_w=window_w, window_h=window_h,
                extra_ffplay=extra_ffplay, log_path=log_path, env=env,
                _force_rebootstrap=True, _retry=_retry - 1,
            )
        raise RuntimeError(f"bridge START_SESSION refused: {reply!r}")

    if not _wait_for_min_bytes(tmpfile, 512 * 1024, timeout=25.0):
        logprint("first session produced no usable output in 25s")
        try: bridge_ctrl.send("QUIT", reply_timeout=1.0)
        except Exception: pass
        try: bridge.terminate()
        except Exception: pass
        # Kill the bridge's process group too — its ffmpeg children
        # may still hold fds on tmpfile, and we're about to spawn a
        # new bridge with fresh cache.
        try: os.killpg(os.getpgid(bridge.pid), 9)
        except Exception: pass
        try: tmpfile.unlink()
        except Exception: pass
        if _retry > 0:
            logprint("first session empty — retrying with fresh bootstrap")
            print("[bridge_player] first session empty — retrying with "
                  "fresh bootstrap", file=sys.stderr, flush=True)
            log.close()
            return start_player(
                video_id, window_title=window_title,
                window_w=window_w, window_h=window_h,
                extra_ffplay=extra_ffplay, log_path=log_path, env=env,
                _force_rebootstrap=True, _retry=_retry - 1,
            )
        raise RuntimeError("first session produced no usable output")
    logprint(f"first session tmpfile ready ({tmpfile.stat().st_size//1024}KB)")

    # 4) IPC socket path for ffplay-yt ↔ bridge_player.
    ipc_socket = Path(tempfile.mkstemp(
        prefix=f"ytlive_{video_id}_ipc_", suffix=".sock")[1])
    ipc_socket.unlink(missing_ok=True)

    # 5) ffplay-yt.
    ffplay_bin = str(FFPLAY_YT if FFPLAY_YT.exists() else "ffplay")
    ffplay_cmd = [ffplay_bin, "-hide_banner", "-loglevel", "warning",
                  "-alwaysontop",
                  "-window_title", window_title,
                  "-ipc", str(ipc_socket)]
    if window_w and window_h and window_w > 100 and window_h > 100:
        ffplay_cmd += ["-x", str(window_w), "-y", str(window_h)]
    if extra_ffplay:
        ffplay_cmd += extra_ffplay
    ffplay_cmd += ["-i", str(tmpfile)]
    logprint(f"ffplay cmd: {shlex.join(ffplay_cmd)}")
    ffplay = subprocess.Popen(ffplay_cmd, cwd=PROJECT_DIR, env=env,
                              stdin=subprocess.DEVNULL,
                              stdout=log, stderr=log)

    # SponsorBlock segments for this video. Fetched once, cached
    # 24 h on disk. Empty list if the API returns 404 (no segments).
    try:
        sponsor_segs = sb_mod.get_segments(video_id)
        if sponsor_segs:
            logprint(f"sponsorblock: {len(sponsor_segs)} segments to skip")
            for s, e, c in sponsor_segs:
                logprint(f"  {c:15s}  {s:7.1f} → {e:7.1f}")
        else:
            logprint("sponsorblock: no segments")
    except Exception as e:
        logprint(f"sponsorblock fetch failed: {e}")
        sponsor_segs = []

    lp = LivePlayer(
        bridge=bridge, bridge_ctrl=bridge_ctrl, ffplay=ffplay,
        tmpfile=tmpfile, ipc_socket=ipc_socket, bridge_socket=bridge_sock,
        log_path=log_path,
        video_id=video_id, window_title=window_title, env=env,
        sponsor_segments=sponsor_segs,
        _session_counter=0,
    )
    lp.start_controller()

    focus_env = dict(env)
    focus_env.setdefault("DISPLAY", os.environ.get("DISPLAY", ":0"))
    threading.Thread(
        target=_xdo_focus_player, args=(window_title, focus_env),
        daemon=True,
    ).start()

    return lp


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: bridge_player.py <video_id|url>", file=sys.stderr)
        return 2
    arg = sys.argv[1]
    vid = _video_id_from_url(arg) if arg.startswith("http") else arg
    title = f"YouTube — {vid}"
    p = start_player(vid, window_title=title)
    try:
        rc = p.wait()
    except KeyboardInterrupt:
        p.kill()
        rc = 130
    print(f"ffplay exit {rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
