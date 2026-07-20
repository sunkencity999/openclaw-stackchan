# StackChan × OpenClaw — embodied agent status + presence

A complete recipe for wiring an [M5StackChan](https://github.com/kisaragi-mochi/stackchan-mcp)
robot (M5Stack CoreS3 / ESP32-S3) to an [OpenClaw](https://openclaw.ai) agent host, so the
little robot becomes the agent's physical surface: it speaks, sees, moves — and acts as a
**live status indicator** for agent activity.

```
StackChan body (ESP32-S3, Wi-Fi)
   │  WS :8775 / photo POST :8776
   ▼
stackchan-mcp gateway (PyPI, MCP streamable-http on 127.0.0.1:8777)
   ├── scripts/stackchan_agent_watch.py   ← agent-status watcher (this repo)
   │     IDLE dim · RUNNING blue pulse · WAITING amber · DONE green+nod · ERROR red flash
   │     + tap-to-acknowledge: tap the head during a done/waiting cue to clear it
   └── scripts/presence_watcher.py        ← presence watcher (camera frame-diff / VAD / BLE)
         greets on arrival; agent-status watcher defers to it
```

Built around **OpenClaw** as the parent tool: the status watcher polls
`openclaw sessions list --json` plus live transcript mtimes to derive agent state,
then drives the body's LEDs/face/servos over MCP. No speech is ever produced by the
status layer — motion is limited to rate-limited nods.

## Install (for another M5Stack owner)

1. **Flash the body** with the stackchan-mcp firmware (see *Firmware* below for the
   exact esptool command; grab `merged-binary.bin` from the upstream Releases page —
   firmware binaries are intentionally not committed here).
2. **Install the gateway** on your Linux host:
   `uv tool install 'stackchan-mcp[tts,stt-faster-whisper]'`
   Create `gateway/.env` (ports, token) and `gateway/token.secret` (chmod 600).
3. **Install units:** copy `systemd/*.service` to `~/.config/systemd/user/`
   (they use `%h`; edit paths if your checkout differs), then
   `systemctl --user daemon-reload && systemctl --user enable --now stackchan-gateway stackchan-agent-watch stackchan-presence`.
4. **Configs:** `agent_status/config.json` is committed and hot-reloaded (edit absolute
   paths for your user). For presence, copy `presence/config.example.json` →
   `presence/config.json` and fill in your own device MACs/IPs (the live file is
   gitignored because it contains personal identifiers).
5. **Scripts:** run the daemons with the gateway venv python
   (`~/.local/share/uv/tools/stackchan-mcp/bin/python`), which has `mcp[client]`.
   The copies in `scripts/` carry a header noting the hardcoded paths to adjust.

### Tap-to-acknowledge (agent_status)

After a DONE (green) or WAITING (amber) cue, the watcher polls the head-touch sensor
(`get_touch_state` MCP verb, Si12T) every 2s for up to 60s — *only* during that window,
never at idle. A tap/stroke clears the cue back to idle immediately and appends a JSON
line to `agent_status/ack.log`. Untouched cues auto-clear on timeout. Tunables live in
`agent_status/config.json` under `cues.ack`.

Detection details (2026-07-19 field fix): a tap fired right as the green cue appears
lands *before* the ack window opens (the done cue plays face/LED/nod first), so the
watcher accepts tap/stroke events up to `cues.ack.grace_seconds` (default 5) before
window start. Short taps typically register as firmware `stroke` events (~400–1900ms);
the instantaneous `zone0..2` booleans rarely catch them at a 2s poll cadence.
Set `cues.ack.touch_debug: true` to log every raw touch response + threshold decision
to `watcher.log` during ack windows.

#### ⚠ LCD-touch (FT6336) gap — 2026-07-19

The LCD is the more intuitive ack surface (bigger, easier to hit than the tiny Si12T
head pads), but **stackchan-mcp 0.10.0 exposes NO screen-touch verb**. Full firmware
tool inventory: 19 forwarded verbs across `self.audio_speaker`, `self.camera`,
`self.display` (avatar/blink/mouth only), `self.led`, `self.robot`,
`self.screen.set_brightness` (that's the only `screen` verb), `self.touch.get_touch_state`
(head Si12T only) — plus `listen`, `say`, `load_avatar_set`, and a few diag verbs. No
`screen_touch`, `get_screen_touch`, `touch_point`, or FT6336-related endpoint.

**What LCD taps actually do (built-in firmware behavior, not our daemon):** the
xiaozhi-esp32 firmware routes LCD taps to `Application::ToggleChatState`, same handler
as the wake word / physical button. First tap enters listening mode (green base-LED
chat-state indicator ON), second tap exits it (green OFF). That is what Christopher
was seeing — it is completely independent of the agent-status daemon's DONE green cue,
and there is no gateway-side signal we can poll for it. The device does emit a
`{"type":"listen","state":"start|stop"}` WebSocket notification, but the gateway only
forwards it when `STACKCHAN_AUDIO_HOOK_URL` is configured (it isn't). ⚠ Collision risk:
firmware and daemon both drive base LEDs, so LCD-toggled green can visually clash with
our DONE green pulse — treat them as separate signals.

**Config knob (present, but only one option works):**
`cues.ack.ack_surface` in `agent_status/config.json`, defaults to `"head"`.
`"screen"` and `"both"` are reserved and currently fall back to `"head"`. To make
screen-ack real would require either patching stackchan-mcp gateway to expose
`get_screen_touch` (firmware would still need a matching handler), or wiring an
audio-hook receiver at `STACKCHAN_AUDIO_HOOK_URL` and treating device-driven
`listen.start` as an ack signal (with the side effect that every LCD tap also enters
firmware listening mode).

### Tap-to-talk voice loop (scaffold — disabled by default)

`scripts/stackchan_voice_loop.py` + `systemd/stackchan-voice-loop.service` (NOT enabled):
long-press the head (zones held ≥1.5s, or a fresh firmware `stroke`) while the
agent-status watcher reports `idle` → chime (`say`) + steady cyan LEDs → gateway
`listen` (~6s, faster-whisper local STT) → transcription routed to the owner's main
OpenClaw session via `openclaw sessions send` as `[voice] <text> [[speak]]` (the
`[[speak]]` marker signals the reply should also go to body TTS). Safety defaults:
`route.send_enabled: false` (dry-run logs the exact command), touch polling gated to
agent-status `idle` only (never competes with ack windows; see the touch-poll budget
warning in the script docstring). Config: copy `voice_loop/config.example.json` →
`voice_loop/config.json` (live config is gitignored — embeds a personal session key).
Enable after review: `systemctl --user enable --now stackchan-voice-loop.service`.

### Repo layout

- `scripts/` — portable copies of the two watcher daemons
- `systemd/` — portable user units (gateway, agent-watch, presence)
- `agent_status/` — status-watcher config (+ runtime state/logs, gitignored)
- `presence/` — presence watcher config example (+ private live config, gitignored)
- `gateway/`, `firmware/`, `logs/`, `photos/`, `voice_samples/`, `avatars/`, `convo/` —
  local-only / gitignored (secrets, media, large binaries, personal session keys)

Licensed MIT (see `LICENSE`). Everything below documents the original build
("Cherubesque" is the agent that lives in this body).

---

# StackChan — Cherubesque body

M5StackChan (CoreS3 / ESP32-S3) wired to Devbox2 as my speak / see / move surface.

## Architecture

```
┌──────────────────┐    Wi-Fi (LAN)    ┌────────────────────────┐
│ StackChan body   │ ◀───────────────▶ │ stackchan-mcp gateway  │
│ ESP32-S3, 8MB    │   WS :8775        │ on Devbox2 10.0.0.100  │
│ camera, mics,    │   HTTP :8776      │ MCP HTTP 127.0.0.1:8777│
│ speaker, servos  │   (photo POST)    └──────────┬─────────────┘
└──────────────────┘                              │
                                                  ▼
                                       ┌────────────────────────┐
                                       │ scripts/stackchan.py   │
                                       │  - status / info       │
                                       │  - say / listen        │
                                       │  - look / face         │
                                       │  - move / gesture      │
                                       │  - leds                │
                                       └────────────────────────┘
                                                  │
                                                  ▼
                                          (Cherubesque)
```

## Hardware identity

- Chip: ESP32-S3 (QFN56) rev v0.2, dual-core LX7 @ 240 MHz
- MAC: `44:1b:f6:e2:0b:4c`
- Flash: 16 MB, PSRAM: 8 MB Quad
- Servos: SCS0009 yaw (360° continuous) + pitch (90° physical; safe 5..85°; our wrapper uses 10..80° as belt-and-suspenders)
- Camera: GC0308 0.3 MP
- Audio: ES7210 mic codec (dual mic) + AW88298 I2S amp (1 W speaker)
- Display: 320×240 IPS + FT6336U cap touch

## Firmware

- **Installed:** `kisaragi-mochi/stackchan-mcp` firmware-v1.10.0 (xiaozhi-esp32 PROJECT_VER 2.2.6 + StackChan custom board)
- **SHA256 of merged-binary.bin:** `02bcdc5e9719e257ad3ac68d7c96ac422a0151614ab858e909e1a635e126357e`
- **Flashed:** 2026-06-10 at 11:18 PDT via `esptool 5.3.0` over `/dev/ttyACM0` at 460800 baud. NVS reset.
- **Local copy:** `firmware/merged-binary.bin`

To reflash:
```bash
sudo ~/.local/bin/esptool.py --chip esp32s3 --port /dev/ttyACM0 -b 460800 \
  write-flash 0x0 ~/.openclaw/workspace/stackchan/firmware/merged-binary.bin
```

Pre-built firmware from upstream Releases page; gateway also lives on PyPI. To preserve NVS on update, flash `xiaozhi.bin` to `0x20000` instead.

## Gateway

- **Package:** `stackchan-mcp[tts,stt-faster-whisper]==0.10.0` installed via `uv tool`
- **Binary:** `/home/sunkencity999/.local/bin/stackchan-mcp`
- **Service:** `stackchan-gateway.service` (systemd --user, linger=yes, enabled, auto-restart)
- **Config:** `gateway/.env` (chmod 600)
- **Logs:** `logs/gateway.log` + `logs/gateway.err.log`
- **Ports:**
  - `ws://0.0.0.0:8775/` — body WebSocket endpoint
  - `http://0.0.0.0:8776/capture` — photo POST endpoint
  - `http://127.0.0.1:8777/mcp` — MCP Streamable HTTP (loopback only)
- **mDNS:** advertises `stackchan-mcp._stackchan-mcp._tcp.local.` so the body auto-discovers
- **Auth:** Bearer token at `gateway/token.secret` (chmod 600), shared with the body via `STACKCHAN_TOKEN`
- **Firewall:** ufw allows 8775 + 8776 from `10.0.0.0/24` only

## TTS / STT engines

- **TTS:** VOICEVOX (Japanese voices). Docker container `stackchan-voicevox` on `127.0.0.1:50021`, `--restart unless-stopped`. Default speaker id 3 (Zundamon normal). **This is a placeholder for the first smoke test.** Phase 3 work: replace the VOICEVOX adapter with an ElevenLabs adapter that uses our existing Nova voice id `CVRACyqNcQefTlxMj9bt`.
- **STT:** faster-whisper, `base.en` model, CPU, int8. Local, MIT, audio never leaves Devbox2.

## Control wrapper

`scripts/stackchan.py` is the thin client. Examples:

```bash
# Use the gateway's bundled Python (has mcp[client]); a venv is optional.
PY=/home/sunkencity999/.local/share/uv/tools/stackchan-mcp/bin/python

$PY scripts/stackchan.py status
$PY scripts/stackchan.py info
$PY scripts/stackchan.py face happy
$PY scripts/stackchan.py say "Hello, Christopher."
$PY scripts/stackchan.py listen --seconds 5
$PY scripts/stackchan.py look                # JPEG path
$PY scripts/stackchan.py move --yaw 30 --pitch 60
$PY scripts/stackchan.py gesture nod
$PY scripts/stackchan.py leds --r 0 --g 80 --b 120
```

Pitch is clamped to 10..80° at our layer (gateway already enforces 5..85°).

## Behavioral layer (live)

- `stackchan-avatar-autoload.service` — auto-loads custom avatar archive on reconnect and forces neutral (`idle`) face.
- `stackchan-idle-motion.service` — continuous slow idle sway + tiny pitch breathing + rare randomized attention twitch.
- `stackchan-reflex.service` — touch acknowledgement reflex (happy face + micro nod + soft LED pulse, cooldown-protected).
- `stackchan-scene-watch.timer` — periodic camera snapshots every 10 minutes with perceptual-hash change logging to:
  - `reports/body_observations/scene_log.jsonl`
  - `memory/stackchan_scene_state.json`
  - `reports/body_observations/captures/YYYY-MM-DD/`
- Semantic change notes (low-frequency): when change is significant and cooldown elapsed, a BEFORE/AFTER diff image is generated and summarized in:
  - `reports/body_observations/scene_notes.jsonl`
  - `reports/body_observations/diffs/YYYY-MM-DD/`

## Presence detection (added 2026-06-13)

Three concurrent signals running as a sidecar (`stackchan-presence.service`):

1. **Frame-diff motion** — calls `take_photo` every ~5s, downsamples to 32×32 grayscale, fires when mean-absolute pixel delta crosses threshold (default 14).
2. **Mic VAD via gateway `listen`** — every ~12s polls a 2s listen window; any returned transcription = audio presence.
3. **BLE scan** — uses Devbox2's internal Intel AX211 (hci0), `bleak` library, allowlist-based.

Fusion: configurable (`any` = single signal triggers, `two_of_three` = require two distinct signal types within 30s). Cooldown prevents re-firing.

Files:
- Daemon: `scripts/presence_watcher.py` (subcommands: `run`, `scan`, `tail`)
- Config: `stackchan/presence/config.json` (hot-reloaded on mtime change)
- Events: `stackchan/presence/logs/events.jsonl`
- State: `stackchan/presence/state.json` (latest motion delta, last greeting, recent signals)
- Systemd: `stackchan-presence.service` (built, disabled — enable after body bring-up)

### ⚠️ BLE identity caveat (read this)

Modern phones (iOS + Android) **rotate their BLE MAC every ~15 minutes** for privacy. Adding a phone MAC to the allowlist will identify it for ~15 min then go silent. Real identity options:

- **Best:** pair Christopher's phone with Devbox2's `hci0` (`bluetoothctl`). Pairing exchanges an Identity Resolving Key (IRK) so rotating MACs resolve to a stable identity.
- **Good:** use stable BLE advertisers that don't rotate — smartwatches, AirPods case, fitness bands. Still proxy presence for "someone who owns these is in the room."
- **Pragmatic:** treat BLE as "any phone-carrying human entered" (always-rotating ads = always-detectable) and let the **camera + Qwen3-VL** do actual identification.

The 8s scan on 2026-06-13 already saw 27 nearby devices including his Quest 3 (`76:B4:C2:E4:A9:A6`) and 65" Samsung TV. Those make decent stable proxies for "in the office."

To survey nearby devices:
```bash
/home/sunkencity999/.local/share/uv/tools/stackchan-mcp/bin/python \
  ~/.openclaw/workspace/scripts/presence_watcher.py scan --seconds 30
```

### Bring-up sequence (post-Wi-Fi onboarding)

1. Confirm body is reachable: `scripts/stackchan.py status` → `connected:true`.
2. Manually exercise `take_photo` and `listen` once each to confirm both round-trip.
3. Edit `stackchan/presence/config.json` — add stable BLE MACs to allowlist (Quest 3 already known); decide fusion rule (`any` first, tighten later).
4. `systemctl --user enable --now stackchan-presence.service`.
5. Tail events: `presence_watcher.py tail`.
6. After 24h, review false-positive rate; tune `threshold_mean_abs_delta` and `rssi_present_threshold_dbm`.

## Bring-up state (live)

| Component | Status |
|---|---|
| Firmware flashed | ✅ verified 2026-06-10 11:18 PDT |
| Gateway service | ✅ `systemctl --user is-active stackchan-gateway` → active |
| VOICEVOX | ✅ Docker container healthy, http 200 |
| mDNS advertising | ✅ `stackchan-mcp._stackchan-mcp._tcp.local.` |
| Wrapper round-trip | ✅ `status` returns `{connected:false,…}` (waiting for body) |
| Body Wi-Fi config | ⏳ Needs Christopher — first boot captive portal |
| Body connected via WS | ⏳ Pending Wi-Fi |
| TTS = Nova (ElevenLabs) | 📋 Phase 3, after smoke test |
| Drive→gesture mapping | 📋 Phase 4 |

## What Christopher does next

1. Power-cycle the body (short-press the power button next to the USB-C on the head, or just unplug+replug).
2. Watch the LCD: it should boot into the xiaozhi-esp32 setup flow. Look for a Wi-Fi configuration screen / QR code.
3. From your phone:
   - Either scan the QR if shown, **or**
   - Open Wi-Fi settings, look for an AP named like `xiaozhi-xxxx` (the device hosts its own AP for first-time setup),
   - Join that AP, your phone opens the captive portal,
   - Enter Christopher's Wi-Fi SSID + password.
4. The device reboots, joins the LAN, mDNS-discovers the gateway at 10.0.0.100, and connects.
5. From Devbox2: `~/.openclaw/workspace/scripts/stackchan.py status` will flip to `connected: true` and show a `device_id`.

After that I can smoke-test say/listen/look/move from Telegram and we're talking.

## Safety reminder

Do not grab and rotate the head when the motors are powered. If the body acts up, power it off via the long-press first.

## Agent-Status Display (added 2026-07-19)

The body doubles as a live OpenClaw activity indicator (Codex-Micro style, but embodied).

- Daemon: `scripts/stackchan_agent_watch.py` (service `stackchan-agent-watch.service`, log + state in `stackchan/agent_status/`)
- Detection: polls `openclaw sessions list --json` for the session roster + `abortedLastRun` (error cue); RUNNING is detected from live transcript-file mtimes under `~/.openclaw/agents/*/sessions/` (excludes `companion` — Esmeralda stays private).
- Cues: idle = very dim teal; running = pulsing blue + thinking face; done (running→quiet 45s) = green pulse + happy face + small nod (nod cooldown ≥5 min, pitch clamped); error = red pulse. **Never speaks.**
- Config hot-reloads: `stackchan/agent_status/config.json` (colors, timings, per-cue enable flags).
- Enable at boot: `systemctl --user enable --now stackchan-agent-watch.service`
- Conflict note: presence watcher LED cues (greeting pulse, poll LED) can overwrite these momentarily; presence wins, agent-status repaints on its next state change.
