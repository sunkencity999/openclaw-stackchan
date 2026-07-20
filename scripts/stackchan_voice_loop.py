#!/usr/bin/env python3
"""StackChan wake-word voice loop — talk to Cherub, prompt routes to OpenClaw.

LIVE as of 2026-07-19 (replaces the long-press scaffold). Say a wake word
("hey cherub" / "cherub" / "okay cherub") toward the body while the
agent-status watcher reports IDLE; the body cues (cyan LEDs, thinking face,
Brian says "Yes?"), captures your prompt (~7 s), and routes the whisper
transcription to Christopher's main OpenClaw Telegram session as:

    [voice] <transcription> [[speak]]

The trailing ``[[speak]]`` marker is the contract with the main agent: a
message carrying it arrived via the body's microphone, so the reply should
ALSO be spoken through the body — e.g. `scripts/stackchan.py say "<reply>"`
(ElevenLabs Brian) — in addition to the normal Telegram text reply.

VAD / wake-word approach (documented per the build task):
  The gateway MCP ``listen`` verb performs the whole capture server-side
  (device Opus frames -> faster-whisper) and returns TEXT ONLY — raw PCM
  never leaves the gateway process (see stackchan/convo/DESIGN.md §1 Path A).
  Client-side webrtcvad on raw frames is therefore impossible on this path.
  Instead we run a bounded short-listen loop: one ~3 s listen window, then a
  deliberate gap, repeated ONLY while the agent-status state machine says
  "idle" and no ack window is open. Wake detection is a normalized-substring
  + fuzzy n-gram match on the transcription (same proven matcher as
  convo_loop.py — catches whisper manglings like "cherub" -> "sherub").

Gateway call budget (hard requirement: << reflex-daemon's ~3/s disaster,
target <= 0.5/s):
  Each wake cycle = 1 listen call taking ~window(3 s) + transcribe(~1 s),
  followed by wake_poll_gap_seconds (default 1.0 s) of silence. Effective
  sustained rate ~= 1 / 5 s = 0.2 calls/sec while idle; ZERO calls while any
  agent is running/waiting or an ack window is open (slow 3 s file-stat gate
  only). The loop logs its measured rate every ~5 min (`rate report`).

Coordination:
  - Only captures when agent_status/state.json == "idle" AND
    agent_status/ack_window.flag is absent (never competes with tap/voice-ack
    windows or presence greetings).
  - Writes voice_loop/capturing.flag while it holds the mic; the agent-status
    watcher skips its voice-ack listens while that flag is fresh.
  - MUTUALLY EXCLUSIVE with stackchan-convo.service (the phase-B convo loop):
    both poll the same mic. Keep exactly one enabled. This daemon refuses to
    capture if convo state.json reports a live non-stopped loop.

Safety / degradation:
  - Gateway/body failures -> 60 s backoff, never crash-loop.
  - Empty / hallucinated ("thank you", "you", ...) / too-short transcripts
    are discarded; a wake with no follow-up prompt aborts quietly.
  - route: ``send_enabled`` in config flips real sends <-> dry-run logging.
  - Cooldown after every capture (default 10 s) so one conversation doesn't
    machine-gun the main session.

Run with the gateway venv python (has mcp[client]):
  /home/sunkencity999/.local/share/uv/tools/stackchan-mcp/bin/python \
      scripts/stackchan_voice_loop.py run
  ... once   # single gate snapshot + one wake window, no send

Config: stackchan/voice_loop/config.json (hot-reload on mtime; defaults
written on first run). Service: stackchan-voice-loop.service.
Disable:  systemctl --user disable --now stackchan-voice-loop.service
Dry-run:  set "send_enabled": false in config.json (hot-reloads in-place).
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import difflib
import json
import os
import re
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
CAPTURING_FLAG = VOICE_DIR / "capturing.flag"
AGENT_STATE_PATH = WORKSPACE / "stackchan" / "agent_status" / "state.json"
ACK_FLAG_PATH = WORKSPACE / "stackchan" / "agent_status" / "ack_window.flag"
CONVO_STATE_PATH = WORKSPACE / "stackchan" / "convo" / "state.json"

GATEWAY_URL = os.environ.get("STACKCHAN_GATEWAY_URL", "http://127.0.0.1:8777/mcp")
TOKEN_PATH = Path(
    os.environ.get(
        "STACKCHAN_TOKEN_PATH",
        str(WORKSPACE / "stackchan" / "gateway" / "token.secret"),
    )
)

DEFAULT_CONFIG: dict[str, Any] = {
    "_comment": (
        "StackChan wake-word voice loop. Hot-reloaded on mtime change. "
        "send_enabled=false => dry-run (exact command logged, not sent)."
    ),
    "enabled": True,
    # Wake detection ---------------------------------------------------------
    "wake_words": ["hey cherub", "okay cherub", "ok cherub", "hey cherubesque", "cherub"],
    "fuzzy_ratio": 0.82,
    "wake_listen_seconds": 3.0,
    "wake_poll_gap_seconds": 1.0,
    # Prompt capture ----------------------------------------------------------
    "prompt_listen_seconds": 7.0,
    "min_prompt_chars": 2,
    "min_prompt_words": 1,
    "ignore": [
        "thank you", "thanks for watching", "thanks", "you", ".", "bye",
        "whoop", "hmm", "uh", "oh", "ok", "okay", "yeah", "bye-bye",
        "thank you for watching", "please subscribe", "silence", "music",
    ],
    # Gating ------------------------------------------------------------------
    "idle_poll_seconds": 3.0,       # gate re-check cadence while NOT idle (no mic)
    "cooldown_seconds": 10.0,
    "stt_model": "base.en",
    "language": "en",
    # Routing -----------------------------------------------------------------
    "send_enabled": True,
    "target_session": "agent:main:telegram:direct:6902857843",
    "prefix": "[voice] ",
    "speak_marker": " [[speak]]",
    "openclaw_bin": "openclaw",
    "deliver_reply": True,
    "cmd_timeout_seconds": 180,
    # Cues --------------------------------------------------------------------
    "listening_cue": {
        "greeting": "Yes?",
        "greeting_enabled": True,
        "led": {"r": 0, "g": 120, "b": 120},
        "face": "thinking",
    },
    "ok_led": {"r": 0, "g": 120, "b": 20},
    "fail_led": {"r": 100, "g": 30, "b": 0},
    "idle_led": {"r": 0, "g": 2, "b": 4},
    "idle_face": "idle",
    "gateway_backoff_seconds": 60,
}

_PUNCT_RE = re.compile(r"[^\w\s']", re.UNICODE)


def log(msg: str, **kw: Any) -> None:
    entry = {"ts": round(time.time(), 3),
             "iso": datetime.now().astimezone().isoformat(timespec="seconds"),
             "msg": msg, **kw}
    line = json.dumps(entry, ensure_ascii=False)
    sys.stderr.write(line + "\n")
    sys.stderr.flush()


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
                    log("config reloaded",
                        send_enabled=bool(merged.get("send_enabled")))

    def get(self, *names: str, default: Any = None) -> Any:
        node: Any = self.raw
        for n in names:
            if not isinstance(node, dict):
                return default
            node = node.get(n)
        return default if node is None else node


# ---------------------------------------------------------------------------
# Gateway MCP client (per-call session, same pattern as agent-watch)
# ---------------------------------------------------------------------------

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


def extract_listen_text(result: Any) -> str:
    """Pull the transcription out of a listen() result (gateway returns the
    JSON blob {engine, text, language, ...} either structured or as text)."""
    def from_blob(v: str) -> str:
        v = v.strip()
        if not v:
            return ""
        try:
            obj = json.loads(v)
        except Exception:
            return v
        if isinstance(obj, dict):
            inner = obj.get("text")
            if isinstance(inner, str):
                return inner.strip()
        return ""

    if isinstance(result, dict):
        v = result.get("text")
        return v.strip() if isinstance(v, str) else ""
    if isinstance(result, str):
        return from_blob(result)
    if isinstance(result, list):
        for item in result:
            if isinstance(item, str):
                t = from_blob(item)
                if t:
                    return t
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                t = from_blob(item["text"])
                if t:
                    return t
    return ""


# ---------------------------------------------------------------------------
# Text helpers (matcher proven in convo_loop.py)
# ---------------------------------------------------------------------------

def normalize(text: str) -> str:
    return _PUNCT_RE.sub(" ", text.lower()).replace("  ", " ").strip()


def wake_match(cfg: Config, transcript: str) -> tuple[bool, str]:
    """Return (matched, remainder-after-wake-word)."""
    norm = normalize(transcript)
    if len(norm) < 3:
        return False, ""
    phrases = [normalize(p) for p in cfg.get("wake_words", default=[]) if p]
    ratio = float(cfg.get("fuzzy_ratio", default=0.82))

    for p in sorted(phrases, key=len, reverse=True):
        idx = norm.find(p)
        if idx >= 0:
            return True, norm[idx + len(p):].strip()

    words = norm.split()
    for p in sorted(phrases, key=len, reverse=True):
        p_len = max(1, len(p.split()))
        for i in range(0, max(1, len(words) - p_len + 1)):
            gram = " ".join(words[i:i + p_len])
            if difflib.SequenceMatcher(None, p, gram).ratio() >= ratio:
                return True, " ".join(words[i + p_len:]).strip()
    return False, ""


def is_real_prompt(cfg: Config, transcript: str) -> bool:
    norm = normalize(transcript)
    if len(norm) < int(cfg.get("min_prompt_chars", default=2)):
        return False
    deny = {normalize(d) for d in cfg.get("ignore", default=[])}
    if norm in deny:
        return False
    return len(norm.split()) >= int(cfg.get("min_prompt_words", default=1))


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------

def agent_state() -> str:
    try:
        st = json.loads(AGENT_STATE_PATH.read_text())
        return str(st.get("state", "unknown"))
    except (OSError, ValueError):
        return "unknown"


def convo_loop_live() -> bool:
    """True if the phase-B convo loop appears to be actively running."""
    try:
        st = json.loads(CONVO_STATE_PATH.read_text())
    except (OSError, ValueError):
        return False
    if str(st.get("state", "stopped")) in ("stopped", "disabled"):
        return False
    pid = st.get("pid")
    if not pid:
        return False
    return Path(f"/proc/{int(pid)}").exists()


def gate_open(cfg: Config) -> tuple[bool, str]:
    if not cfg.get("enabled", default=True):
        return False, "disabled in config"
    if agent_state() != "idle":
        return False, f"agent state={agent_state()}"
    if ACK_FLAG_PATH.exists():
        return False, "ack window open"
    if convo_loop_live():
        return False, "convo loop live (mutually exclusive)"
    return True, "idle"


# ---------------------------------------------------------------------------
# Voice loop
# ---------------------------------------------------------------------------

class VoiceLoop:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.stop = asyncio.Event()
        self.backoff_until = 0.0
        self.cooldown_until = 0.0
        # rate accounting
        self.calls = 0
        self.rate_window_start = time.monotonic()

    # -- mic ownership flag ---------------------------------------------------

    def _flag_up(self, why: str) -> None:
        with contextlib.suppress(OSError):
            CAPTURING_FLAG.write_text(json.dumps({
                "pid": os.getpid(), "why": why,
                "iso": datetime.now().astimezone().isoformat(timespec="seconds"),
            }))

    def _flag_down(self) -> None:
        with contextlib.suppress(OSError):
            CAPTURING_FLAG.unlink(missing_ok=True)

    # -- gateway helpers ------------------------------------------------------

    def _count_call(self) -> None:
        self.calls += 1
        now = time.monotonic()
        if now - self.rate_window_start >= 300:
            rate = self.calls / (now - self.rate_window_start)
            log("rate report", gateway_calls=self.calls,
                window_s=round(now - self.rate_window_start),
                calls_per_sec=round(rate, 3))
            self.calls = 0
            self.rate_window_start = now

    async def listen(self, seconds: float, why: str) -> str | None:
        """One gateway listen window; returns transcript ('' ok) or None on
        gateway failure (backoff armed)."""
        now = time.monotonic()
        if now < self.backoff_until:
            return None
        self._flag_up(why)
        try:
            self._count_call()
            res = await mcp_call("listen", {
                "duration_ms": int(seconds * 1000),
                "language": str(self.cfg.get("language", default="en")),
                "model": str(self.cfg.get("stt_model", default="base.en")),
            }, timeout=seconds + 25)
            return extract_listen_text(res)
        except Exception as exc:
            backoff = float(self.cfg.get("gateway_backoff_seconds", default=60))
            self.backoff_until = time.monotonic() + backoff
            log("listen failed; backing off", why=why, error=str(exc)[:200],
                backoff_s=backoff)
            return None
        finally:
            self._flag_down()

    async def cue(self, led: dict[str, Any] | None = None,
                  face: str | None = None) -> None:
        with contextlib.suppress(Exception):
            if face:
                self._count_call()
                await mcp_call("set_avatar", {"face": face}, timeout=10)
        with contextlib.suppress(Exception):
            if led:
                self._count_call()
                await mcp_call("set_all_leds", {
                    "r": int(led.get("r", 0)), "g": int(led.get("g", 0)),
                    "b": int(led.get("b", 0)),
                }, timeout=10)

    async def say(self, text: str) -> None:
        if not text:
            return
        with contextlib.suppress(Exception):
            self._count_call()
            await mcp_call("say", {"text": text, "voice": "elevenlabs"},
                           timeout=60)

    # -- wake -> prompt -> route ---------------------------------------------

    async def handle_wake(self, remainder: str, wake_heard: str) -> None:
        cue_cfg = self.cfg.get("listening_cue", default={})
        log("wake word detected", heard=wake_heard, remainder=remainder)
        await self.cue(cue_cfg.get("led"), cue_cfg.get("face"))

        prompt = ""
        # Latency win: words spoken after the wake word in the same window
        # already form the prompt ("hey cherub what time is it").
        if remainder and is_real_prompt(self.cfg, remainder):
            prompt = remainder
            log("prompt taken from wake window", prompt=prompt)
        else:
            if cue_cfg.get("greeting_enabled", True):
                await self.say(str(cue_cfg.get("greeting", "Yes?")))
            heard = await self.listen(
                float(self.cfg.get("prompt_listen_seconds", default=7.0)),
                why="prompt")
            log("prompt window transcription", heard=heard)
            if heard and is_real_prompt(self.cfg, heard):
                prompt = heard.strip()

        if not prompt:
            log("no usable prompt after wake; aborting quietly")
            await self.cue(self.cfg.get("fail_led", default={}), None)
            await asyncio.sleep(0.8)
            await self.cue(self.cfg.get("idle_led", default={}),
                           self.cfg.get("idle_face", default="idle"))
            self.cooldown_until = time.monotonic() + 3.0
            return

        ok = self.route(prompt)
        await self.cue(
            self.cfg.get("ok_led" if ok else "fail_led", default={}), None)
        await asyncio.sleep(1.0)
        await self.cue(self.cfg.get("idle_led", default={}),
                       self.cfg.get("idle_face", default="idle"))
        self.cooldown_until = time.monotonic() + float(
            self.cfg.get("cooldown_seconds", default=10.0))

    def route(self, text: str) -> bool:
        # NOTE: there is no `openclaw sessions send` CLI (checked 2026-07-19,
        # OpenClaw 2026.6.10). The supported way to inject a user turn into a
        # stored session is `openclaw agent --session-key ... --message ...`
        # (same mechanism convo_loop.py uses). --deliver sends the agent's
        # reply to the session's Telegram channel so Christopher sees it.
        msg = (f"{self.cfg.get('prefix', default='[voice] ')}{text}"
               f"{self.cfg.get('speak_marker', default=' [[speak]]')}")
        cmd = [str(self.cfg.get("openclaw_bin", default="openclaw")),
               "agent",
               "--session-key", str(self.cfg.get("target_session", default="")),
               "--message", msg, "--json"]
        if self.cfg.get("deliver_reply", default=True):
            cmd.append("--deliver")
        if not self.cfg.get("send_enabled", default=True):
            log("DRY-RUN (send_enabled=false); would send",
                cmd=shlex.join(cmd), transcription=text)
            return True
        try:
            out = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=float(self.cfg.get("cmd_timeout_seconds", default=180)),
            )
            ok = out.returncode == 0
            log("agent turn routed", ok=ok, rc=out.returncode,
                transcription=text, message=msg,
                err=out.stderr[:200] if not ok else "")
            return ok
        except Exception as exc:
            log("agent turn route failed", error=str(exc)[:200],
                transcription=text)
            return False

    # -- main loop ------------------------------------------------------------

    async def run(self) -> None:
        log("wake-word voice loop started", gateway=GATEWAY_URL,
            wake_words=self.cfg.get("wake_words", default=[]),
            send_enabled=bool(self.cfg.get("send_enabled", default=True)))
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stop.set)

        last_gate_reason = ""
        while not self.stop.is_set():
            self.cfg.reload()
            gate_sleep = max(1.0, float(self.cfg.get("idle_poll_seconds", default=3.0)))
            now = time.monotonic()

            if now < self.backoff_until or now < self.cooldown_until:
                await self._sleep(min(gate_sleep,
                                      max(self.backoff_until, self.cooldown_until) - now + 0.1))
                continue

            ok, reason = gate_open(self.cfg)
            if not ok:
                if reason != last_gate_reason:
                    log("gate closed; mic idle", reason=reason)
                    last_gate_reason = reason
                await self._sleep(gate_sleep)
                continue
            if last_gate_reason:
                log("gate open; wake polling resumes")
                last_gate_reason = ""

            heard = await self.listen(
                float(self.cfg.get("wake_listen_seconds", default=3.0)),
                why="wake-poll")
            if heard:
                matched, remainder = wake_match(self.cfg, heard)
                if matched:
                    # Re-check gate right before the interactive capture.
                    if gate_open(self.cfg)[0]:
                        await self.handle_wake(remainder, heard)
                    continue
                log("heard (no wake word)", text=heard[:120])
            await self._sleep(max(0.25, float(
                self.cfg.get("wake_poll_gap_seconds", default=1.0))))

        log("voice loop shutting down")
        self._flag_down()

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.stop.wait(), timeout=seconds)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="daemon loop")
    sub.add_parser("once", help="gate snapshot + one wake window, no send")
    args = ap.parse_args()

    cfg = Config()
    if args.cmd == "once":
        async def _once() -> None:
            vl = VoiceLoop(cfg)
            ok, reason = gate_open(cfg)
            heard = await vl.listen(
                float(cfg.get("wake_listen_seconds", default=3.0)),
                why="once") if ok else None
            matched, remainder = wake_match(cfg, heard or "")
            print(json.dumps({
                "gate_open": ok, "gate_reason": reason,
                "agent_state": agent_state(),
                "heard": heard, "wake_matched": matched,
                "remainder": remainder,
                "send_enabled": bool(cfg.get("send_enabled", default=True)),
            }, indent=2))
        asyncio.run(_once())
        return
    asyncio.run(VoiceLoop(cfg).run())


if __name__ == "__main__":
    main()
