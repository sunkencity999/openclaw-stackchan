#!/usr/bin/env python3
# PORTABLE COPY for the stackchan repo. For a new install, edit the hardcoded
# WORKSPACE / path constants below (they point at /home/sunkencity999/... on
# the original box) or export the STACKCHAN_* env overrides where supported.
# Canonical live copy on the origin machine: ~/.openclaw/workspace/scripts/
"""StackChan presence watcher.

Three independent presence signals run concurrently:

  1. Frame-diff   — periodically calls the gateway's `take_photo` and compares
                    a downsampled grayscale thumb against the previous frame.
                    Mean-absolute pixel delta crossing a threshold = motion.

  2. Mic VAD      — periodically calls the gateway's `listen` for a short
                    window. Any returned transcription text counts as
                    "someone made noise in the room".

  3. BLE scan     — uses bleak on Devbox2's internal Bluetooth radio (Intel
                    AX211, hci0) to scan for advertisements. Devices on the
                    allowlist that are seen above an RSSI threshold count as
                    proximity events; can also identify *who* is present.

A fusion rule decides when to fire a presence event. Events are appended to
`stackchan/presence/logs/events.jsonl` and optionally pinged to Telegram. A
cooldown prevents the body from chirping every few seconds.

Subcommands:
    presence_watcher.py run        # daemon (used by systemd)
    presence_watcher.py scan       # 30s BLE survey, prints devices + RSSI
    presence_watcher.py tail       # follow the events log

Config: /home/sunkencity999/.openclaw/workspace/stackchan/presence/config.json
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths & defaults
# ---------------------------------------------------------------------------

WORKSPACE = Path("/home/sunkencity999/.openclaw/workspace")
PRESENCE_DIR = WORKSPACE / "stackchan" / "presence"
CONFIG_PATH = PRESENCE_DIR / "config.json"
EVENTS_PATH = PRESENCE_DIR / "logs" / "events.jsonl"
STATE_PATH = PRESENCE_DIR / "state.json"

GATEWAY_URL = os.environ.get(
    "STACKCHAN_GATEWAY_URL", "http://127.0.0.1:8777/mcp"
)
TOKEN_PATH = Path(
    os.environ.get(
        "STACKCHAN_TOKEN_PATH",
        str(WORKSPACE / "stackchan" / "gateway" / "token.secret"),
    )
)


def _load_token() -> str | None:
    tok = os.environ.get("STACKCHAN_TOKEN")
    if tok:
        return tok.strip()
    if TOKEN_PATH.exists():
        return TOKEN_PATH.read_text().strip()
    return None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class Config:
    raw: dict[str, Any]
    loaded_at: float
    mtime: float

    @classmethod
    def load(cls) -> "Config":
        mtime = CONFIG_PATH.stat().st_mtime
        raw = json.loads(CONFIG_PATH.read_text())
        return cls(raw=raw, loaded_at=time.time(), mtime=mtime)

    def maybe_reload(self) -> "Config":
        try:
            current_mtime = CONFIG_PATH.stat().st_mtime
        except FileNotFoundError:
            return self
        if current_mtime != self.mtime:
            log("config reloaded")
            return Config.load()
        return self

    def section(self, name: str) -> dict[str, Any]:
        return self.raw.get(name, {}) or {}


# ---------------------------------------------------------------------------
# Logging (single line JSON to stderr + events.jsonl)
# ---------------------------------------------------------------------------


def log(msg: str, **kw: Any) -> None:
    entry = {"ts": time.time(), "level": "info", "msg": msg, **kw}
    sys.stderr.write(json.dumps(entry) + "\n")
    sys.stderr.flush()


def emit_event(kind: str, **kw: Any) -> None:
    EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": time.time(), "kind": kind, **kw}
    with EVENTS_PATH.open("a") as fh:
        fh.write(json.dumps(entry) + "\n")


def write_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_PATH)


# ---------------------------------------------------------------------------
# MCP call (lazy import so --help works without the SDK)
# ---------------------------------------------------------------------------


def _unpack_tool_result(result: Any) -> Any:
    if getattr(result, "structuredContent", None) is not None:
        return result.structuredContent
    return [
        {"type": c.type, "text": getattr(c, "text", None)}
        for c in result.content
    ]


async def mcp_call(tool: str, args: dict[str, Any] | None = None) -> Any:
    """One-shot tool call. Opens a fresh session — expensive (~400ms each).
    Fine for single calls; use `mcp_session()` for multi-call choreography.
    """
    from mcp import ClientSession  # type: ignore
    from mcp.client.streamable_http import streamablehttp_client  # type: ignore

    headers: dict[str, str] = {}
    tok = _load_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    async with streamablehttp_client(GATEWAY_URL, headers=headers) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            result = await session.call_tool(tool, args or {})
            return _unpack_tool_result(result)


@contextlib.asynccontextmanager
async def mcp_session():
    """Persistent MCP session for choreographed sequences (greetings, gestures).
    Yields a `call_tool(tool, args)` async callable bound to one open session,
    so we pay the connect+initialize cost once instead of per-step.
    """
    from mcp import ClientSession  # type: ignore
    from mcp.client.streamable_http import streamablehttp_client  # type: ignore

    headers: dict[str, str] = {}
    tok = _load_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    async with streamablehttp_client(GATEWAY_URL, headers=headers) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()

            async def call_tool(tool: str, args: dict[str, Any] | None = None) -> Any:
                result = await session.call_tool(tool, args or {})
                return _unpack_tool_result(result)

            yield call_tool


# ---------------------------------------------------------------------------
# Signal: frame diff
# ---------------------------------------------------------------------------


def _decode_photo_bytes(result: Any) -> bytes | None:
    """Best-effort extract JPEG/PNG bytes from a take_photo MCP response.

    Body firmware/MCP packaging may evolve; handle multiple shapes:
      - {"image_base64": "..."} / {"data": "..."} / {"image": "..."}
      - {"file_path": "/path/to.jpg"}
      - [{"type": "image", "data": "...", "mimeType": "image/jpeg"}, ...]
    Unknown shapes return None.
    """
    if isinstance(result, dict):
        for k in ("image_base64", "image", "data", "photo_base64"):
            v = result.get(k)
            if isinstance(v, str) and len(v) > 100:
                try:
                    return base64.b64decode(v)
                except Exception:
                    pass
        for k in ("image_path", "file_path", "path", "photo_path"):
            v = result.get(k)
            if isinstance(v, str) and Path(v).is_file():
                return Path(v).read_bytes()
    if isinstance(result, list):
        for item in result:
            if isinstance(item, dict):
                if item.get("type") == "image":
                    v = item.get("data") or item.get("text")
                    if isinstance(v, str):
                        try:
                            return base64.b64decode(v)
                        except Exception:
                            pass
                if item.get("type") == "text" and isinstance(item.get("text"), str):
                    txt = item["text"]
                    # Sometimes the body returns a JSON blob as text
                    try:
                        nested = json.loads(txt)
                        bts = _decode_photo_bytes(nested)
                        if bts:
                            return bts
                    except Exception:
                        pass
    return None


async def frame_diff_loop(cfg_ref: dict[str, Any], fusion: "Fusion") -> None:
    from PIL import Image  # type: ignore
    import numpy as np

    prev_arr: Any = None
    while True:
        cfg = cfg_ref["cfg"].section("frame_diff")
        if not cfg.get("enabled", True):
            await asyncio.sleep(5)
            continue
        try:
            interval = float(cfg.get("interval_seconds", 5))
            thumb = int(cfg.get("thumb_size", 32))
            threshold = float(cfg.get("threshold_mean_abs_delta", 14.0))
            result = await asyncio.wait_for(
                mcp_call("take_photo", {"question": "presence_check"}),
                timeout=15,
            )
            data = _decode_photo_bytes(result)
            if not data:
                log("frame_diff: could not decode photo", shape_keys=list(result.keys()) if isinstance(result, dict) else type(result).__name__)
                await asyncio.sleep(interval)
                continue
            img = Image.open(io.BytesIO(data)).convert("L").resize(
                (thumb, thumb), Image.BILINEAR
            )
            arr = np.asarray(img, dtype="int16")
            if prev_arr is not None:
                delta = float(np.mean(np.abs(arr - prev_arr)))
                fusion.update_latest_motion(delta, threshold)
                if delta >= threshold:
                    emit_event("motion", delta=round(delta, 2), threshold=threshold)
                    await fusion.consider("motion", details={"delta": round(delta, 2)})
            prev_arr = arr
        except Exception as exc:
            log("frame_diff error", error=str(exc))
        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# Signal: mic / listen
# ---------------------------------------------------------------------------


def _extract_transcription(result: Any) -> str | None:
    """Body returns listen results as [{type:'text', text: '<json blob>'}].
    The blob has shape: {engine, text, language, duration_ms, frame_count, sample_rate}.
    We want the *inner* text, not the JSON envelope.
    """
    def _from_json_blob(v: str) -> str | None:
        v = v.strip()
        if not v:
            return None
        try:
            obj = json.loads(v)
            if isinstance(obj, dict):
                inner = obj.get("text")
                if isinstance(inner, str) and inner.strip():
                    return inner.strip()
                # transcription returned no text but ran (frame_count==0 => silence)
                return None
        except Exception:
            return v  # not JSON, treat as literal transcription
        return None

    if isinstance(result, dict):
        for k in ("text", "transcription", "transcript", "speech"):
            v = result.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    if isinstance(result, list):
        for item in result:
            if isinstance(item, dict) and item.get("type") == "text":
                v = item.get("text")
                if isinstance(v, str):
                    parsed = _from_json_blob(v)
                    if parsed:
                        return parsed
    return None


def _is_real_speech(text: str, min_words: int, denylist: list[str]) -> bool:
    """Filter faster-whisper hallucinations on silence."""
    if not text:
        return False
    norm = text.strip().lower().rstrip(".!?,")
    if norm in {d.strip().lower() for d in denylist}:
        return False
    # min_words counts alphabetic tokens of length >=2
    words = [w for w in norm.split() if len(w) >= 2 and any(c.isalpha() for c in w)]
    return len(words) >= min_words


async def mic_vad_loop(cfg_ref: dict[str, Any], fusion: "Fusion") -> None:
    while True:
        cfg = cfg_ref["cfg"].section("mic_vad")
        if not cfg.get("enabled", True):
            await asyncio.sleep(5)
            continue
        try:
            interval = float(cfg.get("interval_seconds", 20))
            listen_s = float(cfg.get("listen_seconds", 4))
            min_words = int(cfg.get("min_words", 2))
            denylist = cfg.get("hallucination_denylist", []) or []
            result = await asyncio.wait_for(
                mcp_call("listen", {"duration_ms": int(listen_s * 1000)}),
                timeout=listen_s + 10,
            )
            text = _extract_transcription(result)
            if text and _is_real_speech(text, min_words, denylist):
                emit_event("audio", text=text)
                await fusion.consider("audio", details={"text": text})
            elif text:
                # logged but suppressed — useful for tuning the denylist
                log("mic_vad: suppressed hallucination", text=text)
        except Exception as exc:
            log("mic_vad error", error=str(exc))
        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# Signal: BLE
# ---------------------------------------------------------------------------


async def ble_scan_loop(cfg_ref: dict[str, Any], fusion: "Fusion") -> None:
    from bleak import BleakScanner  # type: ignore

    seen_rssi: dict[str, deque] = {}
    while True:
        cfg = cfg_ref["cfg"].section("ble")
        if not cfg.get("enabled", True):
            await asyncio.sleep(10)
            continue
        try:
            scan_s = float(cfg.get("scan_interval_seconds", 6))
            allowlist = cfg.get("allowlist", []) or []
            default_thresh = float(cfg.get("rssi_present_threshold_dbm", -75))
            mac_to_entry = {
                str(e.get("mac", "")).upper(): e
                for e in allowlist
                if isinstance(e, dict) and e.get("mac")
            }
            devices = await BleakScanner.discover(
                timeout=scan_s, return_adv=True
            )
            # devices: dict[str(addr) -> (device, adv_data)]
            for addr, (dev, adv) in devices.items():
                mac = (dev.address or addr).upper()
                if mac not in mac_to_entry:
                    continue
                rssi = getattr(adv, "rssi", None)
                if rssi is None:
                    continue
                buf = seen_rssi.setdefault(mac, deque(maxlen=5))
                buf.append(rssi)
                smoothed = sum(buf) / len(buf)
                entry = mac_to_entry[mac]
                thresh = float(
                    entry.get("rssi_present_threshold_dbm", default_thresh)
                )
                if smoothed >= thresh:
                    name = entry.get("name", mac)
                    emit_event(
                        "ble_proximity",
                        mac=mac,
                        name=name,
                        rssi=rssi,
                        smoothed_rssi=round(smoothed, 1),
                    )
                    await fusion.consider(
                        "ble", identity=name, details={"rssi": round(smoothed, 1)}
                    )
        except Exception as exc:
            log("ble_scan error", error=str(exc))
            await asyncio.sleep(5)


async def wifi_presence_loop(cfg_ref: dict[str, Any], fusion: "Fusion") -> None:
    """Track known devices on the LAN (e.g. Christopher's S24 Ultra).

    Wi-Fi presence is an *identity* signal, not a room-occupancy signal:
    the phone being on the network means the person is HOME, while the
    camera/frame-diff says someone is IN THE OFFICE. Fusion combines them:
    motion + phone-home => greet by name.

    Detection: ping the device IP, then confirm the ARP entry matches the
    expected MAC (guards against DHCP reassigning the IP to another device).
    Phones in deep sleep can drop pings, so we only flip to 'away' after
    `miss_tolerance` consecutive failed polls.
    """
    misses: dict[str, int] = {}
    home: dict[str, bool] = {}
    while True:
        cfg = cfg_ref["cfg"].section("wifi")
        if not cfg.get("enabled", False):
            await asyncio.sleep(15)
            continue
        poll_s = float(cfg.get("poll_interval_seconds", 20))
        tolerance = int(cfg.get("miss_tolerance", 4))
        devices = cfg.get("devices", []) or []
        try:
            for dev in devices:
                if not isinstance(dev, dict) or not dev.get("ip"):
                    continue
                ip = str(dev["ip"])
                name = str(dev.get("name", ip))
                identity = str(dev.get("identity", name))
                want_mac = str(dev.get("mac", "")).lower()

                proc = await asyncio.create_subprocess_exec(
                    "ping", "-c", "1", "-W", "2", ip,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                alive = (await proc.wait()) == 0

                mac_ok = True
                if alive and want_mac:
                    try:
                        neigh = await asyncio.create_subprocess_exec(
                            "ip", "neigh", "show", ip,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.DEVNULL,
                        )
                        out, _ = await neigh.communicate()
                        mac_ok = want_mac in out.decode().lower()
                    except Exception:
                        mac_ok = True  # best-effort; don't lose the signal

                if alive and mac_ok:
                    misses[ip] = 0
                    if not home.get(ip):
                        home[ip] = True
                        emit_event("wifi_home", ip=ip, name=name, identity=identity)
                        log("wifi device home", ip=ip, name=name)
                    fusion.set_wifi_identity(identity)
                else:
                    misses[ip] = misses.get(ip, 0) + 1
                    if home.get(ip) and misses[ip] >= tolerance:
                        home[ip] = False
                        emit_event("wifi_away", ip=ip, name=name, identity=identity)
                        log("wifi device away", ip=ip, name=name,
                            missed_polls=misses[ip])
                        fusion.clear_wifi_identity(identity)
        except Exception as exc:
            log("wifi_presence error", error=str(exc) or repr(exc))
        await asyncio.sleep(poll_s)


# ---------------------------------------------------------------------------
# Fusion: decide when a presence event fires
# ---------------------------------------------------------------------------


@dataclass
class Fusion:
    """Occupancy state machine.

    States: ABSENT | PRESENT.

    - Any incoming signal refreshes `last_signal_ts`.
    - If state is ABSENT, the first qualifying signal transitions to PRESENT
      and (subject to greeting cooldown) fires `_on_presence` once.
    - While PRESENT, additional signals just refresh occupancy — no re-greet.
    - A background sweeper transitions PRESENT → ABSENT after
      `absence_seconds_before_reset` of no signal.
    """

    cfg_ref: dict[str, Any]
    state: str = "absent"
    last_signal_ts: float = 0.0
    presence_started_ts: float = 0.0
    last_greeting_ts: float = 0.0
    recent_signals: deque = field(default_factory=lambda: deque(maxlen=10))
    latest_motion_delta: float = 0.0
    wifi_identities: set = field(default_factory=set)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def set_wifi_identity(self, identity: str) -> None:
        self.wifi_identities.add(identity)

    def clear_wifi_identity(self, identity: str) -> None:
        self.wifi_identities.discard(identity)

    def _write_state(self, threshold: float | None = None) -> None:
        write_state(
            {
                "occupancy_state": self.state,
                "latest_motion_delta": round(self.latest_motion_delta, 2),
                "motion_threshold": threshold,
                "presence_started_iso": time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(self.presence_started_ts)
                ) if self.presence_started_ts else None,
                "last_signal_iso": time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(self.last_signal_ts)
                ) if self.last_signal_ts else None,
                "last_greeting_iso": time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(self.last_greeting_ts)
                ) if self.last_greeting_ts else None,
                "recent_signals": list(self.recent_signals),
            }
        )

    def update_latest_motion(self, delta: float, threshold: float) -> None:
        self.latest_motion_delta = delta
        self._write_state(threshold)

    async def consider(
        self,
        signal: str,
        identity: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        async with self._lock:
            cfg = self.cfg_ref["cfg"].section("fusion")
            cooldown = float(cfg.get("greeting_cooldown_seconds", 900))
            rule = cfg.get("confirmed_presence_requires", "any")

            now = time.time()

            # Identity enrichment: anonymous room signals (motion) get named
            # when a known person's phone is on the LAN.
            if (identity is None or identity == "unknown") and self.wifi_identities:
                if len(self.wifi_identities) == 1:
                    identity = next(iter(self.wifi_identities))

            self.recent_signals.append(
                {"ts": now, "signal": signal, "identity": identity}
            )

            if rule == "two_of_three":
                window = 30.0
                recent_types = {
                    s["signal"] for s in self.recent_signals if now - s["ts"] <= window
                }
                qualified = len(recent_types) >= 2
            else:
                qualified = True

            if not qualified:
                return

            # Always refresh — a qualifying signal means someone is here right now.
            self.last_signal_ts = now

            if self.state == "present":
                # Already known to be in the room. No greeting, just stay warm.
                self._write_state()
                return

            # ABSENT -> PRESENT transition.
            self.state = "present"
            self.presence_started_ts = now
            emit_event(
                "occupancy_transition",
                from_state="absent",
                to_state="present",
                signal=signal,
                identity=identity or "unknown",
                details=details,
            )

            if now - self.last_greeting_ts < cooldown:
                log(
                    "presence detected but greeting suppressed by cooldown",
                    cooldown_remaining_s=round(
                        cooldown - (now - self.last_greeting_ts), 1
                    ),
                )
                self._write_state()
                return

            self.last_greeting_ts = now
            identity_label = identity or "unknown"
            emit_event(
                "presence_confirmed",
                signal=signal,
                identity=identity_label,
                details=details,
            )
            self._write_state()
            await self._on_presence(signal, identity_label)

    async def sweep_absence(self) -> None:
        """Drop PRESENT -> ABSENT after a quiet stretch. Runs forever."""
        while True:
            try:
                cfg = self.cfg_ref["cfg"].section("fusion")
                quiet_s = float(cfg.get("absence_seconds_before_reset", 300))
                await asyncio.sleep(min(30.0, max(5.0, quiet_s / 6)))
                async with self._lock:
                    if self.state == "present" and self.last_signal_ts:
                        idle = time.time() - self.last_signal_ts
                        if idle >= quiet_s:
                            self.state = "absent"
                            emit_event(
                                "occupancy_transition",
                                from_state="present",
                                to_state="absent",
                                idle_seconds=round(idle, 1),
                            )
                            log(
                                "occupancy reset to absent",
                                idle_seconds=round(idle, 1),
                            )
                            self._write_state()
            except Exception as exc:
                log("sweep_absence error", error=str(exc) or repr(exc))
                await asyncio.sleep(5)

    async def _on_presence(self, signal: str, identity: str) -> None:
        # Fire the body greeting in the background so it doesn't stall
        # the watcher loop (TTS + nod can take several seconds).
        greet_cfg = self.cfg_ref["cfg"].section("body_greeting")
        if greet_cfg.get("enabled", True):
            asyncio.create_task(
                _perform_body_greeting(greet_cfg, identity),
                name="body_greeting",
            )

        cfg = self.cfg_ref["cfg"].section("on_presence")
        if cfg.get("send_telegram"):
            template = cfg.get(
                "telegram_template",
                "👀 Presence detected ({signal}, identity: {identity})",
            )
            text = template.format(signal=signal, identity=identity)
            chat_id = str(cfg.get("telegram_chat_id", "")).strip()
            if chat_id:
                try:
                    openclaw_bin = os.environ.get(
                        "OPENCLAW_BIN",
                        "/home/sunkencity999/.local/bin/openclaw",
                    )
                    proc = await asyncio.create_subprocess_exec(
                        openclaw_bin,
                        "message",
                        "send",
                        "--channel",
                        "telegram",
                        "--target",
                        chat_id,
                        "--message",
                        text,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    _, err = await asyncio.wait_for(proc.communicate(), timeout=60)
                    if proc.returncode != 0:
                        log("telegram notify failed", err=err.decode(errors="replace"))
                except Exception as exc:
                    log("telegram notify exception", error=str(exc) or repr(exc))


# ---------------------------------------------------------------------------
# Body greeting
# ---------------------------------------------------------------------------


async def _perform_body_greeting(cfg: dict[str, Any], identity: str) -> None:
    """Choreographed greeting: blink → LED fade-in + surprised face → happy face
    + speak → soft nod → LED fade-out → return to idle.

    Each step is best-effort; if one tool fails the rest still run.
    """
    face = str(cfg.get("face", "happy"))
    opening_face = str(cfg.get("opening_face", "")).strip() or None
    opening_hold = float(cfg.get("opening_face_hold_seconds", 0.5))
    blink_enabled = bool(cfg.get("blink_enabled", True))
    return_after = float(cfg.get("return_to_idle_after_seconds", 8))

    led = cfg.get("led_pulse") or {}
    led_r = int(led.get("r", 255))
    led_g = int(led.get("g", 140))
    led_b = int(led.get("b", 40))
    led_hold = float(led.get("hold_seconds", 1.8))
    led_fade_in_ms = int(led.get("fade_in_ms", 0))
    led_fade_out_ms = int(led.get("fade_out_ms", 0))
    led_steps = max(2, int(led.get("steps", 12)))

    nod_cfg = cfg.get("nod")
    if isinstance(nod_cfg, bool):
        nod_enabled = nod_cfg
        nod_path = [
            {"pitch": 42, "hold_ms": 350},
            {"pitch": 30, "hold_ms": 0},
        ]
    else:
        nod_cfg = nod_cfg or {}
        nod_enabled = bool(nod_cfg.get("enabled", True))
        nod_path = nod_cfg.get("path") or [
            {"pitch": 42, "hold_ms": 350},
            {"pitch": 30, "hold_ms": 0},
        ]

    speech_map = cfg.get("speech_by_identity") or {}
    speech = (
        speech_map.get(identity)
        or cfg.get("default_speech")
        or "Hello."
    )
    say_voice = str(cfg.get("tts_engine", "elevenlabs"))

    log("body greeting starting", identity=identity, speech=speech)

    async with mcp_session() as call_tool:
        async def _try(tool: str, args: dict[str, Any], timeout: float = 30.0) -> None:
            try:
                await asyncio.wait_for(call_tool(tool, args), timeout=timeout)
            except Exception as exc:
                log("greeting step failed", tool=tool, error=str(exc) or repr(exc))

        async def _led_ramp(
            r0: int, g0: int, b0: int,
            r1: int, g1: int, b1: int,
            total_ms: int, steps: int,
        ) -> None:
            if total_ms <= 0:
                await _try("set_all_leds", {"r": r1, "g": g1, "b": b1}, timeout=5)
                return
            step_ms = total_ms / steps
            for i in range(1, steps + 1):
                t = i / steps
                r = int(r0 + (r1 - r0) * t)
                g = int(g0 + (g1 - g0) * t)
                b = int(b0 + (b1 - b0) * t)
                await _try("set_all_leds", {"r": r, "g": g, "b": b}, timeout=5)
                if step_ms > 0:
                    await asyncio.sleep(step_ms / 1000.0)

        # 1) Make sure autonomous blinking is on so the face feels alive.
        if blink_enabled:
            await _try("set_blink", {"enabled": True}, timeout=5)

        # 2) Opening beat: surprised face + LED fade-in in parallel.
        opening_tasks = [
            asyncio.create_task(
                _led_ramp(0, 0, 0, led_r, led_g, led_b, led_fade_in_ms, led_steps)
            )
        ]
        if opening_face:
            opening_tasks.append(
                asyncio.create_task(_try("set_avatar", {"face": opening_face}, timeout=5))
            )
        await asyncio.gather(*opening_tasks, return_exceptions=True)

        if opening_face and opening_hold > 0:
            await asyncio.sleep(opening_hold)

        # 3) Main face
        await _try("set_avatar", {"face": face}, timeout=5)

        # 4) Speak — TTS is the slowest step; let it complete before nodding.
        await _try("say", {"text": speech, "voice": say_voice}, timeout=30)

        # 5) Soft multi-point nod.
        if nod_enabled:
            for step in nod_path:
                try:
                    pitch = int(step.get("pitch", 30))
                except (TypeError, ValueError):
                    continue
                await _try("move_head", {"yaw": 0, "pitch": pitch}, timeout=5)
                hold_ms = int(step.get("hold_ms", 0))
                if hold_ms > 0:
                    await asyncio.sleep(hold_ms / 1000.0)

        # 6) Hold the LED warmth a beat, then fade everything home.
        await asyncio.sleep(max(0.0, led_hold))
        if led_fade_out_ms > 0:
            await _led_ramp(led_r, led_g, led_b, 0, 0, 0, led_fade_out_ms, led_steps)
        await _try("clear_leds", {}, timeout=5)

        # 7) Optionally return to idle face after a longer pause.
        elapsed = led_hold + (led_fade_out_ms / 1000.0)
        if return_after > elapsed:
            await asyncio.sleep(return_after - elapsed)
            await _try("set_avatar", {"face": "idle"}, timeout=5)

    log("body greeting done", identity=identity)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


async def cmd_run() -> int:
    if not CONFIG_PATH.exists():
        log("config missing", path=str(CONFIG_PATH))
        return 2
    cfg_ref: dict[str, Any] = {"cfg": Config.load()}
    fusion = Fusion(cfg_ref=cfg_ref)

    async def reloader() -> None:
        while True:
            await asyncio.sleep(15)
            cfg_ref["cfg"] = cfg_ref["cfg"].maybe_reload()

    tasks = [
        asyncio.create_task(reloader(), name="reloader"),
        asyncio.create_task(frame_diff_loop(cfg_ref, fusion), name="frame_diff"),
        asyncio.create_task(mic_vad_loop(cfg_ref, fusion), name="mic_vad"),
        asyncio.create_task(ble_scan_loop(cfg_ref, fusion), name="ble"),
        asyncio.create_task(wifi_presence_loop(cfg_ref, fusion), name="wifi"),
        asyncio.create_task(fusion.sweep_absence(), name="absence_sweep"),
    ]

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    log("presence_watcher started", config=str(CONFIG_PATH))
    await stop.wait()
    log("presence_watcher stopping")
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    return 0


async def cmd_scan(seconds: int) -> int:
    from bleak import BleakScanner  # type: ignore

    print(f"Scanning BLE for {seconds}s...")
    devices = await BleakScanner.discover(timeout=seconds, return_adv=True)
    rows = []
    for addr, (dev, adv) in devices.items():
        name = (dev.name or adv.local_name or "").strip() or "(unknown)"
        rssi = getattr(adv, "rssi", None)
        rows.append((rssi if rssi is not None else -999, dev.address, name))
    rows.sort(reverse=True)
    print(f"{'RSSI':>6}  {'MAC':<20}  NAME")
    print("-" * 60)
    for rssi, mac, name in rows:
        print(f"{rssi:>6}  {mac:<20}  {name}")
    return 0


def cmd_tail() -> int:
    if not EVENTS_PATH.exists():
        print(f"no events yet at {EVENTS_PATH}")
        return 0
    try:
        subprocess.run(["tail", "-F", str(EVENTS_PATH)])
    except KeyboardInterrupt:
        pass
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="run the watcher daemon")
    s = sub.add_parser("scan", help="one-off BLE scan to identify devices")
    s.add_argument("--seconds", type=int, default=30)
    sub.add_parser("tail", help="tail the presence events log")
    return p


def main() -> int:
    args = build_parser().parse_args()
    if args.cmd == "run":
        return asyncio.run(cmd_run())
    if args.cmd == "scan":
        return asyncio.run(cmd_scan(args.seconds))
    if args.cmd == "tail":
        return cmd_tail()
    return 1


if __name__ == "__main__":
    sys.exit(main())
