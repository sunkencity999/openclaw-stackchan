# StackChan Body Upgrade — Build Plan (approved 2026-07-15 23:14)

Christopher greenlit the full roadmap: items 1-4 plus my additions, "complete freedom to
operate and spend tokens." Presence beacon switched from Quest 3 → **S24 Ultra** (always
on him).

## Recon findings (2026-07-15 night)
- BLE scan: 30+ devices, phone MAC randomized/rotating → **BLE unsuitable for phone identity**.
- Decision: **Wi-Fi presence primary** — Android per-SSID MAC is persistent on BradfordHomeBase.
  Find S24's LAN IP/MAC via ARP sweep when phone is home (it was NOT obviously in `ip neigh`
  from Devbox2 tonight; 10.0.0.119 = Samsung TV, not phone). May need router DHCP table or
  `nmap -sn 10.0.0.0/24` while he's home.
- Gateway + presence services: both active. Presence venv python:
  `~/.local/share/uv/tools/stackchan-mcp/bin/python` (bleak lives there, NOT system python).
- body_greeting currently `enabled: false` — re-enable as part of Phase A.

## Build phases (order matters)

### Phase A — Presence personalization (small, do first)
1. ARP/nmap sweep to identify S24 Ultra on LAN (Samsung OUI or hostname; confirm with
   Christopher if ambiguous).
2. Add `wifi_presence` signal to presence_watcher.py: poll `ip neigh` / ping the S24 IP
   every ~30s; REACHABLE = home. (Room-level vs home-level: wifi says HOME, frame_diff
   says IN OFFICE; fusion: wifi + frame_diff within window ⇒ identity=Christopher.)
3. Update speech_by_identity, re-enable body_greeting.

### Phase B — Voice conversation loop (the centerpiece)
1. **WebRTC-VAD gate** in front of faster-whisper (fixes the known hallucination problem
   that forced mic_vad off on 2026-06-13). `webrtcvad` py pkg, aggressiveness 2-3,
   require ≥ ~500ms voiced frames before transcribing.
2. **Wake word**: try on-device first (xiaozhi-esp32 base has ESP-SR wake word on the S3;
   check stackchan-mcp firmware config/docs for exposed wake-word settings). Fallback:
   gateway-side openWakeWord on Devbox2 chewing `listen` audio. Target phrase: "Hey Cherub"
   (or whatever custom phrase ESP-SR/openWakeWord supports; openWakeWord allows custom
   training).
3. **Conversation state machine** (new script `stackchan/convo/convo_loop.py` + systemd unit):
   wake → chime/face cue → record until VAD silence → whisper STT → route to MAIN agent
   session (openclaw CLI or gateway API so body-talk = same continuity as Telegram) →
   ElevenLabs Brian TTS out speaker → 8s follow-up listen window → sleep.
   Latency budget: STT <1s (base.en), agent turn variable, TTS ~1-2s. Speak a brief
   "thinking" acknowledgment if agent turn >5s.
4. Barge-in/stop phrase: "never mind" / "stop" cancels.

### Phase C — Sight loop (Phase 4 from original roadmap)
1. Wire `look` verb → vision.py ask (Qwen3-VL on :8105, swap-on-demand).
2. In convo loop: if utterance references looking/seeing ("look at this", "what do you see"),
   auto-capture + VL describe, feed into agent turn.

### Phase D — Face tracking
1. Periodic camera frames → cheap face/person detection (opencv haar or ultralight onnx on
   CPU; do NOT burn VL swaps for this) → yaw servo steps toward subject.
2. Rate-limit servo moves; respect idle-motion service (coordinate or pause it during tracking).

### Phase E — Expression & rhythm extras
1. Drive-linked micro-gestures (curiosity tilt, connection LED pulse, stewardship settle).
2. Spoken morning brief: presence-detected settle-in 07:00-10:00 window + not-yet-delivered
   ⇒ offer brief aloud.
3. Office journal: 2-3 glances/day logged to memory/office_journal.md (like webcam practice).

## Constraints / notes
- Speaker tests during day only (body is in the office; late-night testing = talking robot at 2am).
- All audio processing local (whisper + VAD on Devbox2); ElevenLabs is the only cloud hop (TTS out).
- Don't fight stackchan-idle-motion.service — coordinate.
- mic hardware: dual mics on CoreS3, accessed via gateway `listen` tool (returns wav).
- Firmware repo: kisaragi-mochi/stackchan-mcp v1.10.0 (based on xiaozhi-esp32 2.2.6).

## Status log
- 2026-07-15 23:20 — Plan written. BLE recon done. Build starts 2026-07-16 morning
  (after Mother Harriet twins dispatch at 08:00).
- 2026-07-15 23:45 — **Phase B built** (subagent): `stackchan/convo/` — convo_loop.py
  (poll + hook modes), vad_gate.py, test_vad_gate.py, config.json, DESIGN.md,
  stackchan-convo.service (written, NOT enabled). Key findings: (1) MCP `listen`
  returns TEXT only — raw audio only flows via STACKCHAN_AUDIO_HOOK_URL
  (device-driven listens; unset today, needs .env line + gateway restart), so
  poll mode wakes on transcript text match and the VAD gate lives in hook mode;
  (2) WebRTC VAD alone is NOT the hallucination fix — pink noise scores 100%
  voiced at aggressiveness 3; added Silero VAD (bundled in openwakeword) as
  stage 2 → speech PASS 0.986 / pink noise REJECT 0.045 / brown REJECT 0.114,
  and openWakeWord scores 0.9986 on a rendered "Hey Jarvis" clip; (3) agent
  routing verified: `openclaw agent --session-key <main> --json` → reply in
  .result.payloads[].text, 6.4 s trivial turn (holding phrase at 5 s will fire).
  Placeholder wake phrase "hey jarvis" (pretrained model in convo/models/);
  quiet hours 23–08 baked in. Live testing checklist in convo/DESIGN.md §8.
  Gateway/presence services untouched.
