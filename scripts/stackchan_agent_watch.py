#!/usr/bin/env python3
# PORTABLE COPY for the stackchan repo. For a new install, edit the hardcoded
# WORKSPACE / path constants below (they point at /home/sunkencity999/... on
# the original box) or export the STACKCHAN_* env overrides where supported.
# Canonical live copy on the origin machine: ~/.openclaw/workspace/scripts/
"""StackChan agent-status display daemon.

Turns the StackChan body into a live status indicator for OpenClaw agent
activity — the physical-robot equivalent of RGB "agent keys":

  IDLE     — very dim LEDs, neutral face
  RUNNING  — slow pulsing blue LEDs, thinking face (any session recently active)
  WAITING  — steady amber + surprised face (touch-file hook: waiting.flag)
  DONE     — brief green + happy face + small nod (rate-limited), back to idle
  ERROR    — red flash x3 (session with abortedLastRun inside error window)

Tap-to-acknowledge (phase 2, 2026-07-19):
  After a DONE or WAITING cue fires, an *ack window* opens (default 60s).
  During the window ONLY, the head-touch sensor (`get_touch_state` MCP verb,
  Si12T) is polled at a slow cadence (default every 2s => max ~30 polls per
  cue). A tap/stroke inside the window clears the cue immediately (idle-dim
  LEDs, neutral face) and appends a line to agent_status/ack.log. If
  untouched, the window times out and the cue auto-clears as before.
  ⚠ NEVER poll touch outside an ack window — the old reflex daemon was
  killed 2026-07-16 for continuous ~3 sessions/sec touch polling.

State sources (both read-only):
  1. Transcript file mtimes (~/.openclaw/agents/*/sessions/*.jsonl) — these are
     appended live while an agent turn is in progress, so mtime age is the
     real "running right now" signal. (`sessions list` updatedAt only
     refreshes at turn boundaries — discovered during build, 2026-07-19.)
  2. `openclaw sessions list --json --all-agents --active N` — roster mapping
     sessionId -> session key, plus abortedLastRun for the ERROR cue.

The `companion` agent (Esmeralda) is excluded by default — her store is
private to Antonia (see exclude_agents in config).

Design rules:
  - NO speech/TTS ever. Motion = small nods only, >=5 min cooldown.
  - LED/face messages sent only on state change (plus a slow 1-call/2.5s
    breath while RUNNING).
  - Presence watcher wins: after a presence greeting we go quiet for a bit.
  - Degrades gracefully: gateway/body/CLI failures log + back off, never crash.

Usage:
  stackchan_agent_watch.py run          # daemon (systemd or foreground)
  stackchan_agent_watch.py once         # single poll, print derived state
Run with the gateway venv python (has mcp[client]):
  /home/sunkencity999/.local/share/uv/tools/stackchan-mcp/bin/python ...

Config: stackchan/agent_status/config.json (hot-reloaded on mtime change).
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

WORKSPACE = Path("/home/sunkencity999/.openclaw/workspace")
STATUS_DIR = WORKSPACE / "stackchan" / "agent_status"
CONFIG_PATH = STATUS_DIR / "config.json"
STATE_PATH = STATUS_DIR / "state.json"

GATEWAY_URL = os.environ.get("STACKCHAN_GATEWAY_URL", "http://127.0.0.1:8777/mcp")
TOKEN_PATH = Path(
    os.environ.get(
        "STACKCHAN_TOKEN_PATH",
        str(WORKSPACE / "stackchan" / "gateway" / "token.secret"),
    )
)

PITCH_MIN, PITCH_MAX = 5, 80
NEUTRAL_PITCH = int(os.environ.get("STACKCHAN_NEUTRAL_PITCH", "7"))


def log(msg: str, **kw: Any) -> None:
    entry = {"ts": round(time.time(), 3), "msg": msg, **kw}
    sys.stderr.write(json.dumps(entry, ensure_ascii=False) + "\n")
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# Config (hot-reload on mtime)
# ---------------------------------------------------------------------------

class Config:
    def __init__(self) -> None:
        self.raw: dict[str, Any] = {}
        self.mtime = 0.0
        self.reload(force=True)

    def reload(self, force: bool = False) -> None:
        try:
            mtime = CONFIG_PATH.stat().st_mtime
        except FileNotFoundError:
            if force:
                raise
            return
        if force or mtime != self.mtime:
            self.raw = json.loads(CONFIG_PATH.read_text())
            self.mtime = mtime
            if not force:
                log("config reloaded")

    def sec(self, *names: str) -> dict[str, Any]:
        node: Any = self.raw
        for n in names:
            node = (node or {}).get(n, {})
        return node or {}


# ---------------------------------------------------------------------------
# Gateway MCP client (per-call session, same pattern as stackchan.py)
# ---------------------------------------------------------------------------

def _load_token() -> str | None:
    tok = os.environ.get("STACKCHAN_TOKEN")
    if tok:
        return tok.strip()
    if TOKEN_PATH.exists():
        return TOKEN_PATH.read_text().strip()
    return None


async def mcp_call(tool: str, args: dict[str, Any] | None = None) -> Any:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers: dict[str, str] = {}
    tok = _load_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    async with streamablehttp_client(GATEWAY_URL, headers=headers) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            res = await session.call_tool(tool, args or {})
            if res.structuredContent is not None:
                return res.structuredContent
            # Some verbs (e.g. get_touch_state) return JSON only as text
            # content with structuredContent=null — parse it as a fallback.
            for block in res.content or []:
                text = getattr(block, "text", None)
                if text:
                    try:
                        return json.loads(text)
                    except (ValueError, TypeError):
                        return text
            return None


class Body:
    """Cue sender with change-only dedup + failure backoff."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.last_led: tuple[int, int, int] | None = None
        self.last_face: str | None = None
        self.backoff_until = 0.0

    def _deferring(self) -> bool:
        # Presence watcher wins: quiet period after its greeting.
        coord = self.cfg.sec("coordination")
        path = coord.get("presence_state_path")
        defer_s = float(coord.get("defer_after_greeting_seconds", 45))
        if not path:
            return False
        try:
            st = json.loads(Path(path).read_text())
            iso = st.get("last_greeting_iso")
            if not iso:
                return False
            ts = datetime.fromisoformat(iso)
            if ts.tzinfo is None:
                ts = ts.astimezone()
            age = (datetime.now(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds()
            return 0 <= age < defer_s
        except Exception:
            return False

    async def _call(self, tool: str, args: dict[str, Any]) -> bool:
        now = time.monotonic()
        if now < self.backoff_until:
            return False
        if self._deferring():
            log("deferring to presence greeting; cue skipped", tool=tool)
            return False
        try:
            await asyncio.wait_for(mcp_call(tool, args), timeout=15)
            return True
        except Exception as exc:  # gateway down / body offline / timeout
            backoff = float(self.cfg.sec("coordination").get("gateway_backoff_seconds", 60))
            self.backoff_until = now + backoff
            self.last_led = None  # force resend once healthy again
            self.last_face = None
            log("gateway call failed; backing off", tool=tool,
                error=str(exc)[:200], backoff_s=backoff)
            return False

    async def leds(self, color: dict[str, Any], force: bool = False) -> None:
        rgb = (int(color.get("r", 0)), int(color.get("g", 0)), int(color.get("b", 0)))
        if not force and rgb == self.last_led:
            return
        if await self._call("set_all_leds", {"r": rgb[0], "g": rgb[1], "b": rgb[2]}):
            self.last_led = rgb

    async def face(self, expression: str) -> None:
        if expression == self.last_face:
            return
        if await self._call("set_avatar", {"face": expression}):
            self.last_face = expression

    async def nod(self) -> None:
        neutral = max(PITCH_MIN, min(PITCH_MAX, NEUTRAL_PITCH))
        up = max(PITCH_MIN, min(PITCH_MAX, neutral + 10))
        if await self._call("move_head", {"yaw": 0, "pitch": up}):
            await asyncio.sleep(0.35)
            await self._call("move_head", {"yaw": 0, "pitch": neutral})

    async def touch_state(self) -> dict[str, Any] | None:
        """Read the head-touch sensor. Returns the structured dict or None.

        Respects the same backoff window as cue sends, but does NOT check
        the presence-defer gate (reading a sensor is not a cue).
        """
        now = time.monotonic()
        if now < self.backoff_until:
            return None
        try:
            res = await asyncio.wait_for(mcp_call("get_touch_state", {}), timeout=10)
            if isinstance(res, dict):
                return res
            return None
        except Exception as exc:
            backoff = float(self.cfg.sec("coordination").get("gateway_backoff_seconds", 60))
            self.backoff_until = now + backoff
            log("touch poll failed; backing off", error=str(exc)[:200], backoff_s=backoff)
            return None


# ---------------------------------------------------------------------------
# OpenClaw session polling (read-only)
# ---------------------------------------------------------------------------

def scan_transcripts(cfg: Config) -> dict[str, float]:
    """Map sessionId -> mtime-age-ms for live transcript files.

    Transcript .jsonl files are appended per tool call / message while a run
    is in progress, so a fresh mtime means an agent is working *right now*.
    Excludes .trajectory.jsonl, lock files, and excluded agents' stores.
    """
    import glob as _glob

    p = cfg.sec("poll")
    pattern = p.get(
        "transcripts_glob",
        "/home/sunkencity999/.openclaw/agents/*/sessions/*.jsonl",
    )
    excluded = set(p.get("exclude_agents", ["companion"]))
    now = time.time()
    out: dict[str, float] = {}
    for path in _glob.glob(pattern):
        if path.endswith(".trajectory.jsonl") or path.endswith(".lock"):
            continue
        parts = Path(path).parts
        try:
            agent = parts[parts.index("agents") + 1]
        except (ValueError, IndexError):
            agent = ""
        if agent in excluded:
            continue
        try:
            age_ms = (now - os.stat(path).st_mtime) * 1000.0
        except OSError:
            continue
        if age_ms < 900_000:  # only track recently-touched files
            out[Path(path).name.replace(".jsonl", "")] = age_ms
    return out


def poll_sessions(cfg: Config) -> list[dict[str, Any]] | None:
    p = cfg.sec("poll")
    cmd = p.get("sessions_cmd") or [
        "openclaw", "sessions", "list", "--json", "--all-agents",
        "--active", "10", "--limit", "all",
    ]
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=float(p.get("cmd_timeout_seconds", 20)),
        )
        if out.returncode != 0:
            log("sessions cmd nonzero", rc=out.returncode, err=out.stderr[:200])
            return None
        data = json.loads(out.stdout)
        sessions = data.get("sessions", [])
        pats = [re.compile(x) for x in p.get("ignore_key_patterns", [])]
        return [s for s in sessions if not any(rx.search(s.get("key", "")) for rx in pats)]
    except Exception as exc:
        log("sessions poll failed", error=str(exc)[:200])
        return None


def waiting_flag_active(cfg: Config) -> bool:
    w = cfg.sec("cues", "waiting")
    if not w.get("enabled", False):
        return False
    path = Path(w.get("flag_path", str(STATUS_DIR / "waiting.flag")))
    try:
        age = time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return False
    if age > float(w.get("stale_seconds", 900)):
        with contextlib.suppress(OSError):
            path.unlink()
        log("stale waiting.flag removed", age_s=round(age))
        return False
    return True


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

class Watcher:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.body = Body(cfg)
        self.state = "startup"           # idle | running | waiting
        self.running_keys: set[str] = set()   # sessions seen active this episode
        self.error_seen: dict[str, int] = {}  # key -> updatedAt already alerted
        self.last_nod = 0.0
        self.last_done = 0.0
        self.last_error_pulse = 0.0
        self.pulse_high = False
        self.last_pulse_step = 0.0
        self.stop = asyncio.Event()
        # Tap-to-ack window: None when idle (NO touch polling happens then).
        # {"cue": str, "start": mono, "deadline": mono, "next_poll": mono}
        self.ack: dict[str, Any] | None = None

    # -- derived state ------------------------------------------------------

    def derive(
        self,
        sessions: list[dict[str, Any]],
        transcript_ages: dict[str, float],
    ) -> tuple[str, bool, list[dict]]:
        """Returns (base_state, done_edge, error_sessions)."""
        p = self.cfg.sec("poll")
        running_age = int(p.get("running_age_ms", 30000))
        quiet_ms = int(p.get("done_quiet_ms", 45000))
        err_window = int(p.get("error_window_ms", 120000))

        id_to_key = {s.get("sessionId"): s["key"] for s in sessions}
        # RUNNING = any transcript touched within running_age (live signal),
        # OR sessions-list updatedAt inside the window (turn-boundary signal).
        active = {
            id_to_key.get(sid, sid)
            for sid, age in transcript_ages.items() if age < running_age
        }
        active |= {s["key"] for s in sessions if s.get("ageMs", 1e12) < running_age}
        errors = [
            s for s in sessions
            if s.get("abortedLastRun") and s.get("ageMs", 1e12) < err_window
            and self.error_seen.get(s["key"]) != s.get("updatedAt")
        ]

        done_edge = False
        if active:
            self.running_keys |= active
        elif self.running_keys:
            # everything quiet — completed once ALL previously-running keys
            # have been quiet for done_quiet_ms (checked against BOTH signals)
            key_to_id = {v: k for k, v in id_to_key.items()}
            def _quiet(key: str) -> bool:
                t_age = transcript_ages.get(key_to_id.get(key, ""), 1e12)
                s_age = next(
                    (s.get("ageMs", 1e12) for s in sessions if s["key"] == key),
                    1e12,
                )
                return min(t_age, s_age) >= quiet_ms
            if all(_quiet(k) for k in self.running_keys):
                done_edge = True
                self.running_keys.clear()

        if waiting_flag_active(self.cfg):
            return "waiting", done_edge, errors
        return ("running" if active else "idle"), done_edge, errors

    # -- cue playback --------------------------------------------------------

    async def enter_idle(self) -> None:
        c = self.cfg.sec("cues", "idle")
        if c.get("enabled", True):
            await self.body.face(c.get("face", "idle"))
            await self.body.leds(c.get("led", {"r": 0, "g": 0, "b": 0}))

    async def enter_running(self) -> None:
        c = self.cfg.sec("cues", "running")
        if c.get("enabled", True):
            await self.body.face(c.get("face", "thinking"))
            await self.body.leds(c.get("led_low", {"r": 0, "g": 10, "b": 40}))
            self.pulse_high = False
            self.last_pulse_step = time.monotonic()

    async def pulse_running(self) -> None:
        c = self.cfg.sec("cues", "running")
        step = float(c.get("pulse_step_seconds", 2.5))
        now = time.monotonic()
        if now - self.last_pulse_step < step:
            return
        self.last_pulse_step = now
        self.pulse_high = not self.pulse_high
        color = c.get("led_high") if self.pulse_high else c.get("led_low")
        await self.body.leds(color or {}, force=True)

    async def enter_waiting(self) -> None:
        c = self.cfg.sec("cues", "waiting")
        await self.body.face(c.get("face", "surprised"))
        await self.body.leds(c.get("led", {"r": 150, "g": 70, "b": 0}))
        self.open_ack("waiting")

    # -- tap-to-acknowledge ---------------------------------------------------

    def _ack_cfg(self) -> dict[str, Any]:
        return self.cfg.sec("cues", "ack")

    def open_ack(self, cue: str) -> None:
        a = self._ack_cfg()
        if not a.get("enabled", False) or cue not in a.get("cues", []):
            return
        now = time.monotonic()
        poll = max(2.0, float(a.get("poll_interval_seconds", 2)))
        self.ack = {
            "cue": cue,
            "start": now,
            "deadline": now + float(a.get("timeout_seconds", 60)),
            "next_poll": now + poll,
        }
        log("ack window opened", cue=cue,
            timeout_s=float(a.get("timeout_seconds", 60)), poll_s=poll)

    def cancel_ack(self, reason: str) -> None:
        if self.ack:
            log("ack window cancelled", cue=self.ack["cue"], reason=reason)
            self.ack = None

    def _ack_log(self, cue: str) -> None:
        a = self._ack_cfg()
        path = Path(a.get("log_path", str(STATUS_DIR / "ack.log")))
        line = json.dumps({
            "ts_iso": datetime.now().astimezone().isoformat(timespec="seconds"),
            "event": "ack",
            "cue": cue,
        }, ensure_ascii=False)
        with contextlib.suppress(OSError):
            with path.open("a") as fh:
                fh.write(line + "\n")

    async def ack_tick(self) -> None:
        """One rate-limited touch poll while an ack window is open.

        Max ~timeout/poll_interval polls per cue (default 60/2 = 30), then
        the window closes and NO further touch traffic happens. This is the
        hard guard against re-creating the reflex-daemon polling flood.
        """
        if not self.ack:
            return
        now = time.monotonic()
        if now >= self.ack["deadline"]:
            cue = self.ack["cue"]
            self.ack = None
            log("ack window timed out; auto-clearing", cue=cue)
            if cue == "done":
                await self.enter_idle()
                self.state = "idle"
            # waiting: leave the amber cue in place (existing flag semantics)
            return
        if now < self.ack["next_poll"]:
            return
        a = self._ack_cfg()
        # ack_surface: which touch surface(s) count as an ack.
        #   "head"   — head Si12T only (default, only working option 2026-07-19)
        #   "screen" — LCD FT6336 (STUB: no MCP verb exists; requires an audio
        #              hook + firmware ToggleChatState detour — see LCD-touch
        #              gap doc in stackchan/README.md). Falls back to "head".
        #   "both"   — currently identical to "head" until screen path lands.
        surface = str(a.get("ack_surface", "head")).lower()
        if surface not in ("head", "screen", "both"):
            surface = "head"
        debug = bool(a.get("touch_debug", False))
        self.ack["next_poll"] = now + max(2.0, float(a.get("poll_interval_seconds", 2)))
        st = await self.body.touch_state()
        if not st or not st.get("available"):
            if debug:
                log("DEBUG ack touch poll: no/unavailable state", cue=self.ack["cue"],
                    raw_response=st)
            return
        elapsed = time.monotonic() - self.ack["start"]
        age_s = float(st.get("last_event_age_ms", 1e15)) / 1000.0
        # Absolute (monotonic) time the last touch event fired. Compare against
        # the window start minus a grace period: play_done sends face/LED/nod
        # BEFORE opening the window (~1.5-2s), so a tap right when the green
        # cue appears lands slightly before ack.start. The old strict
        # `age_s < elapsed` check could NEVER catch those taps (the age stayed
        # permanently ahead of elapsed) — 2026-07-19 field-failure root cause.
        grace = float(a.get("grace_seconds", 5.0))
        event_mono = time.monotonic() - age_s
        fresh_event = (
            st.get("last_event") in ("tap", "stroke")
            and event_mono >= self.ack["start"] - grace
        )
        zones = bool(st.get("zone0") or st.get("zone1") or st.get("zone2"))
        touched = zones or fresh_event
        if debug:
            log("DEBUG ack touch poll", cue=self.ack["cue"], raw_response=st,
                elapsed_s=round(elapsed, 1), age_s=round(age_s, 1),
                grace_s=grace, zones=zones, fresh_event=fresh_event,
                touched=touched)
        if not touched:
            return
        cue = self.ack["cue"]
        self.ack = None
        log("cue acknowledged by touch", cue=cue,
            last_event=st.get("last_event"), age_s=round(age_s, 1))
        self._ack_log(cue)
        if cue == "waiting":
            w = self.cfg.sec("cues", "waiting")
            flag = Path(w.get("flag_path", str(STATUS_DIR / "waiting.flag")))
            with contextlib.suppress(OSError):
                flag.unlink()
        # Clear immediately: idle-dim LEDs + neutral face, re-derive next pass.
        await self.enter_idle()
        self.state = "idle"

    async def play_done(self) -> None:
        c = self.cfg.sec("cues", "done")
        now = time.monotonic()
        if not c.get("enabled", True):
            return
        if now - self.last_done < float(c.get("event_cooldown_seconds", 60)):
            log("done pulse suppressed (event cooldown)")
            return
        self.last_done = now
        log("done event: green pulse")
        await self.body.face(c.get("face", "happy"))
        await self.body.leds(c.get("led", {"r": 0, "g": 120, "b": 20}), force=True)
        if c.get("nod_enabled", True) and (
            now - self.last_nod >= float(c.get("nod_cooldown_seconds", 300))
        ):
            self.last_nod = now
            await self.body.nod()
        a = self._ack_cfg()
        if a.get("enabled", False) and "done" in a.get("cues", []):
            # Ack mode: hold the green cue and open the touch window; the
            # main loop clears it on tap or on ack timeout.
            self.open_ack("done")
            if self.ack:
                self.state = "done-ack"
                return
        await asyncio.sleep(float(c.get("hold_seconds", 5)))
        self.state = "startup"  # force re-entry cue on next loop

    async def play_error(self, sessions: list[dict]) -> None:
        c = self.cfg.sec("cues", "error")
        for s in sessions:
            self.error_seen[s["key"]] = s.get("updatedAt")
        now = time.monotonic()
        if not c.get("enabled", True):
            return
        if now - self.last_error_pulse < float(c.get("cooldown_seconds", 300)):
            return
        self.last_error_pulse = now
        self.cancel_ack("error cue supersedes")
        log("error event: red pulse", keys=[s["key"] for s in sessions])
        color = c.get("led", {"r": 160, "g": 0, "b": 0})
        for i in range(int(c.get("flashes", 3))):
            await self.body.leds(color, force=True)
            await asyncio.sleep(float(c.get("on_seconds", 0.4)))
            await self.body.leds({"r": 0, "g": 0, "b": 0}, force=True)
            if i < int(c.get("flashes", 3)) - 1:
                await asyncio.sleep(float(c.get("off_seconds", 0.3)))
        self.state = "startup"  # restore whatever base state applies

    # -- persistence / loop ---------------------------------------------------

    def save_state(self, base: str) -> None:
        with contextlib.suppress(OSError):
            STATE_PATH.write_text(json.dumps({
                "state": base,
                "updated_iso": datetime.now().astimezone().isoformat(timespec="seconds"),
                "running_keys": sorted(self.running_keys),
                "last_nod_age_s": round(time.monotonic() - self.last_nod) if self.last_nod else None,
            }, indent=2))

    async def run(self) -> None:
        log("agent-status watcher started", gateway=GATEWAY_URL)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stop.set)

        interval = float(self.cfg.sec("poll").get("interval_seconds", 12))
        next_session_poll = 0.0
        while not self.stop.is_set():
            now = time.monotonic()
            if now >= next_session_poll:
                self.cfg.reload()
                interval = float(self.cfg.sec("poll").get("interval_seconds", 12))
                sessions = poll_sessions(self.cfg)
                if sessions is not None:
                    ages = scan_transcripts(self.cfg)
                    base, done_edge, errors = self.derive(sessions, ages)

                    if errors:
                        await self.play_error(errors)
                    if done_edge:
                        await self.play_done()
                        base, _, _ = self.derive(sessions, ages)

                    hold_for_ack = False
                    if self.ack:
                        if self.ack["cue"] == "done":
                            if base == "idle":
                                hold_for_ack = True  # keep green up during window
                            else:
                                self.cancel_ack(f"state changed to {base}")
                                self.state = "startup"  # re-enter proper cue below
                        elif self.ack["cue"] == "waiting" and base != "waiting":
                            self.cancel_ack(f"state changed to {base}")

                    if not hold_for_ack:
                        if base != self.state:
                            log("state change", prev=self.state, new=base)
                            if base == "idle":
                                await self.enter_idle()
                            elif base == "running":
                                await self.enter_running()
                            elif base == "waiting":
                                await self.enter_waiting()
                            self.state = base
                        elif base == "running":
                            await self.pulse_running()
                    self.save_state("done-ack" if hold_for_ack else base)
                next_session_poll = time.monotonic() + interval

            if self.ack:
                await self.ack_tick()

            # Sleep until the next session poll — or the next ack touch poll
            # if a window is open. NO touch traffic outside ack windows.
            now = time.monotonic()
            timeout = next_session_poll - now
            if self.ack:
                timeout = min(
                    timeout,
                    self.ack["next_poll"] - now,
                    self.ack["deadline"] - now,
                )
            timeout = max(0.2, timeout)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.stop.wait(), timeout=timeout)

        log("shutting down; restoring idle cue")
        self.ack = None
        await self.enter_idle()


# ---------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="daemon loop")
    sub.add_parser("once", help="single poll; print derived state, no cues")
    args = ap.parse_args()

    cfg = Config()
    if args.cmd == "once":
        sessions = poll_sessions(cfg)
        if sessions is None:
            sys.exit("poll failed")
        ages = scan_transcripts(cfg)
        w = Watcher(cfg)
        base, done, errors = w.derive(sessions, ages)
        print(json.dumps({
            "state": base, "done_edge": done,
            "errors": [s["key"] for s in errors],
            "running_keys": sorted(w.running_keys),
            "transcript_ages_s": {
                k: round(v / 1000) for k, v in sorted(ages.items(), key=lambda x: x[1])
            },
            "sessions_in_window": len(sessions),
        }, indent=2))
        return
    asyncio.run(Watcher(cfg).run())


if __name__ == "__main__":
    main()
