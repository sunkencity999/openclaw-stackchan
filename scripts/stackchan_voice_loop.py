#!/usr/bin/env python3
# PORTABLE COPY for the stackchan repo. For a new install, edit the hardcoded
# WORKSPACE / path constants below (they point at /home/sunkencity999/... on
# the original box) or export the STACKCHAN_* env overrides where supported.
# Canonical live copy on the origin machine: ~/.openclaw/workspace/scripts/
"""StackChan tap-to-talk voice loop (PART B scaffold, 2026-07-19 — NOT ENABLED).

Long-press the head while the agent-status watcher is IDLE → the body chimes,
shows a steady cyan "listening" cue, captures ~6s of mic audio via the
gateway's `listen` verb (faster-whisper STT, local), and routes the
transcription into Christopher's main OpenClaw Telegram session as:

    [voice] <transcription> [[speak]]

The `[[speak]]` marker tells the responding agent the reply should also be
piped to the body's TTS (ElevenLabs Brian) — a companion hook can watch for
it, or the main agent can act on it directly.

Gesture disambiguation vs tap-to-ack (stackchan_agent_watch.py):
  - SHORT tap during a done/waiting cue  → ack (agent-watch owns that; this
    daemon never polls unless agent-status state == "idle").
  - LONG press (zones held >= long_press_seconds across consecutive polls)
    or a fresh firmware "stroke" event while idle → voice capture.

⚠ TOUCH-POLL BUDGET WARNING (read before enabling):
  The old reflex daemon was killed 2026-07-16 for continuous ~3 calls/sec
  touch polling. This daemon polls get_touch_state at poll_interval_seconds
  (default 0.5s = 2 calls/sec) but ONLY while agent_status/state.json says
  "idle"; any other state (running / waiting / done-ack) drops to a slow
  gate check (idle_gate_seconds, default 3s) with NO touch traffic.
  If gateway load is a concern, raise poll_interval_seconds to 1.0.

Safety: the actual `openclaw sessions send` is gated behind config
  "send_enabled": false  (default) — dry-run logs the exact command instead.
Flip it after review.

Run with the gateway venv python (has mcp[client]):
  /home/sunkencity999/.local/share/uv/tools/stackchan-mcp/bin/python \
      scripts/stackchan_voice_loop.py run
  ... once   # single status snapshot (gate state + touch read), no capture

Config: stackchan/voice_loop/config.json (hot-reloaded on mtime change;
created with defaults on first run). Systemd unit exists but is NOT enabled:
  systemctl --user enable --now stackchan-voice-loop.service   # when approved
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

WORKSPACE = Path("/home/sunkencity999/.openclaw/workspace")
VOICE_DIR = WORKSPACE / "stackchan" / "voice_loop"
CONFIG_PATH = VOICE_DIR / "config.json"
LOG_PATH = VOICE_DIR / "voice.log"
AGENT_STATE_PATH = WORKSPACE / "stackchan" / "agent_status" / "state.json"

GATEWAY_URL = os.environ.get("STACKCHAN_GATEWAY_URL", "http://127.0.0.1:8777/mcp")
TOKEN_PATH = Path(
    os.environ.get(
        "STACKCHAN_TOKEN_PATH",
        str(WORKSPACE / "stackchan" / "gateway" / "token.secret"),
    )
)

DEFAULT_CONFIG: dict[str, Any] = {
    "_comment": (
        "StackChan tap-to-talk voice loop. Hot-reloaded on mtime change. "
        "send_enabled=false keeps sessions-send in dry-run (logged, not sent)."
    ),
    "enabled": True,
    "poll_interval_seconds": 0.5,
    "idle_gate_seconds": 3.0,
    "long_press_seconds": 1.5,
    "stroke_trigger": True,
    "stroke_fresh_ms": 1500,
    "listen_duration_ms": 6000,
    "listen_engine": "faster-whisper",
    "cooldown_seconds": 15,
    "chime": {"enabled": True, "text": "Yes?"},
    "cues": {
        "listening_led": {"r": 0, "g": 120, "b": 120},
        "listening_face": "thinking",
        "ok_led": {"r": 0, "g": 120, "b": 20},
        "fail_led": {"r": 120, "g": 40, "b": 0},
        "idle_led": {"r": 0, "g": 2, "b": 4},
        "idle_face": "idle",
    },
    "route": {
        "send_enabled": False,
        "session_key": "agent:main:telegram:direct:6902857843",
        "prefix": "[voice] ",
        "speak_marker": " [[speak]]",
        "openclaw_bin": "openclaw",
        "cmd_timeout_seconds": 30,
    },
    "min_transcript_chars": 2,
}


def log(msg: str, **kw: Any) -> None:
    entry = {"ts": round(time.time(), 3), "msg": msg, **kw}
    line = json.dumps(entry, ensure_ascii=False)
    sys.stderr.write(line + "\n")
    sys.stderr.flush()
    with contextlib.suppress(OSError):
        with LOG_PATH.open("a") as fh:
            fh.write(line + "\n")


class Config:
    def __init__(self) -> None:
        self.raw: dict[str, Any] = dict(DEFAULT_CONFIG)
        self.mtime = 0.0
        VOICE_DIR.mkdir(parents=True, exist_ok=True)
        if not CONFIG_PATH.exists():
            CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n")
        self.reload(force=True)

    def reload(self, force: bool = False) -> None:
        try:
            mtime = CONFIG_PATH.stat().st_mtime
        except FileNotFoundError:
            return
        if force or mtime != self.mtime:
            with contextlib.suppress(ValueError, OSError):
                merged = dict(DEFAULT_CONFIG)
                merged.update(json.loads(CONFIG_PATH.read_text()))
                self.raw = merged
                self.mtime = mtime
                if not force:
                    log("config reloaded")

    def get(self, *names: str, default: Any = None) -> Any:
        node: Any = self.raw
        for n in names:
            if not isinstance(node, dict):
                return default
            node = node.get(n)
        return default if node is None else node


def _load_token() -> str | None:
    tok = os.environ.get("STACKCHAN_TOKEN")
    if tok:
        return tok.strip()
    if TOKEN_PATH.exists():
        return TOKEN_PATH.read_text().strip()
    return None


async def mcp_call(tool: str, args: dict[str, Any] | None = None,
                   timeout: float = 15.0) -> Any:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers: dict[str, str] = {}
    tok = _load_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"

    async def _inner() -> Any:
        async with streamablehttp_client(GATEWAY_URL, headers=headers) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                res = await session.call_tool(tool, args or {})
                if res.structuredContent is not None:
                    return res.structuredContent
                for block in res.content or []:
                    text = getattr(block, "text", None)
                    if text:
                        try:
                            return json.loads(text)
                        except (ValueError, TypeError):
                            return text
                return None

    return await asyncio.wait_for(_inner(), timeout=timeout)


def agent_state() -> str:
    """Read the agent-status watcher's derived state; unknown => not idle."""
    try:
        st = json.loads(AGENT_STATE_PATH.read_text())
        return str(st.get("state", "unknown"))
    except (OSError, ValueError):
        return "unknown"


class VoiceLoop:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.stop = asyncio.Event()
        self.held_since: float | None = None   # zones continuously true since
        self.backoff_until = 0.0
        self.cooldown_until = 0.0

    # -- touch gating ---------------------------------------------------------

    async def touch(self) -> dict[str, Any] | None:
        now = time.monotonic()
        if now < self.backoff_until:
            return None
        try:
            res = await mcp_call("get_touch_state", {}, timeout=10)
            return res if isinstance(res, dict) else None
        except Exception as exc:
            self.backoff_until = now + 60
            log("touch poll failed; backing off 60s", error=str(exc)[:200])
            return None

    def long_press(self, st: dict[str, Any]) -> str | None:
        """Return trigger reason if the long-press gesture fired."""
        now = time.monotonic()
        zones = bool(st.get("zone0") or st.get("zone1") or st.get("zone2"))
        if zones:
            if self.held_since is None:
                self.held_since = now
            elif now - self.held_since >= float(self.cfg.get("long_press_seconds", default=1.5)):
                self.held_since = None
                return "zones-held"
        else:
            self.held_since = None
        if self.cfg.get("stroke_trigger", default=True):
            age = float(st.get("last_event_age_ms", 1e15))
            if st.get("last_event") == "stroke" and age <= float(
                self.cfg.get("stroke_fresh_ms", default=1500)
            ):
                return "fresh-stroke"
        return None

    # -- capture + route ------------------------------------------------------

    async def cue(self, led: dict[str, Any] | None, face: str | None) -> None:
        with contextlib.suppress(Exception):
            if face:
                await mcp_call("set_avatar", {"face": face})
            if led:
                await mcp_call("set_all_leds", {
                    "r": int(led.get("r", 0)), "g": int(led.get("g", 0)),
                    "b": int(led.get("b", 0)),
                })

    async def capture(self, reason: str) -> None:
        cues = self.cfg.get("cues", default={})
        log("long-press detected; starting capture", reason=reason)
        await self.cue(cues.get("listening_led"), cues.get("listening_face"))
        if self.cfg.get("chime", "enabled", default=True):
            with contextlib.suppress(Exception):
                await mcp_call("say", {"text": str(self.cfg.get("chime", "text", default="Yes?"))},
                               timeout=20)
        dur = int(self.cfg.get("listen_duration_ms", default=6000))
        text = ""
        try:
            res = await mcp_call("listen", {
                "duration_ms": dur,
                "engine": str(self.cfg.get("listen_engine", default="faster-whisper")),
            }, timeout=max(30.0, dur / 1000 + 20))
            if isinstance(res, dict):
                if res.get("error"):
                    log("listen returned error", error=str(res["error"])[:200])
                text = str(res.get("text") or res.get("transcription") or "").strip()
            elif isinstance(res, str):
                text = res.strip()
        except Exception as exc:
            log("listen failed", error=str(exc)[:200])

        if len(text) >= int(self.cfg.get("min_transcript_chars", default=2)):
            ok = self.route(text)
            await self.cue(cues.get("ok_led") if ok else cues.get("fail_led"), None)
        else:
            log("empty/too-short transcription; discarded", text=text)
            await self.cue(cues.get("fail_led"), None)
        await asyncio.sleep(1.2)
        await self.cue(cues.get("idle_led"), cues.get("idle_face"))
        self.cooldown_until = time.monotonic() + float(
            self.cfg.get("cooldown_seconds", default=15))

    def route(self, text: str) -> bool:
        r = self.cfg.get("route", default={})
        msg = f"{r.get('prefix', '[voice] ')}{text}{r.get('speak_marker', ' [[speak]]')}"
        cmd = [str(r.get("openclaw_bin", "openclaw")), "sessions", "send",
               str(r.get("session_key", "")), msg]
        entry = {
            "ts_iso": datetime.now().astimezone().isoformat(timespec="seconds"),
            "transcription": text,
            "message": msg,
        }
        if not r.get("send_enabled", False):
            log("DRY-RUN (route.send_enabled=false); would send",
                cmd=shlex.join(cmd), **entry)
            return True
        try:
            out = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=float(r.get("cmd_timeout_seconds", 30)),
            )
            ok = out.returncode == 0
            log("sessions send", ok=ok, rc=out.returncode,
                err=out.stderr[:200] if not ok else "", **entry)
            return ok
        except Exception as exc:
            log("sessions send failed", error=str(exc)[:200], **entry)
            return False

    # -- main loop ------------------------------------------------------------

    async def run(self) -> None:
        log("voice loop started (scaffold)", gateway=GATEWAY_URL,
            send_enabled=bool(self.cfg.get("route", "send_enabled", default=False)))
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stop.set)

        while not self.stop.is_set():
            self.cfg.reload()
            poll = max(0.25, float(self.cfg.get("poll_interval_seconds", default=0.5)))
            gate = max(1.0, float(self.cfg.get("idle_gate_seconds", default=3.0)))
            now = time.monotonic()

            if not self.cfg.get("enabled", default=True) or now < self.cooldown_until:
                await self._sleep(gate)
                continue
            # Gate: only ever touch-poll while the agent-status watcher says
            # idle — never compete with an active ack window (done-ack /
            # waiting states) and never add load while agents are running.
            if agent_state() != "idle":
                self.held_since = None
                await self._sleep(gate)
                continue

            st = await self.touch()
            if st and st.get("available"):
                reason = self.long_press(st)
                if reason:
                    # Re-check the gate right before capturing.
                    if agent_state() == "idle":
                        await self.capture(reason)
                        continue
                    self.held_since = None
            await self._sleep(poll)

        log("voice loop shutting down")

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.stop.wait(), timeout=seconds)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="daemon loop")
    sub.add_parser("once", help="single gate+touch snapshot, no capture")
    args = ap.parse_args()

    cfg = Config()
    if args.cmd == "once":
        async def _once() -> None:
            vl = VoiceLoop(cfg)
            st = await vl.touch()
            print(json.dumps({
                "agent_state": agent_state(),
                "gate_open": agent_state() == "idle",
                "touch": st,
                "send_enabled": bool(cfg.get("route", "send_enabled", default=False)),
            }, indent=2))
        asyncio.run(_once())
        return
    asyncio.run(VoiceLoop(cfg).run())


if __name__ == "__main__":
    main()
