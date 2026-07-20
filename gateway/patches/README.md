# Local gateway patches (stackchan-mcp)

These patches apply to the installed `stackchan-mcp` uv tool
(`~/.local/share/uv/tools/stackchan-mcp/lib/python3.12/site-packages/stackchan_mcp/`).
**Any `uv tool upgrade stackchan-mcp` wipes them — re-apply and check for the
marker comments listed below.**

| Patch | File | Marker | What it does |
|---|---|---|---|
| `vad-early-stop-and-timings.patch` | `stt/orchestrator.py` | `local-patch: vad-early-stop` | Optional `vad_early_stop` argument on the `listen` tool: energy-based VAD polls the Opus frame buffer and ends the capture on end-of-speech (trailing-silence) or no-speech-onset instead of always burning the full `duration_ms`. Also adds `timings: {capture_ms, transcribe_ms}` + `vad_stopped_early` to the listen result for latency instrumentation. Tunables per-call: `vad_speech_rms` (default 500), `vad_silence_ms` (700), `vad_min_speech_ms` (240), `vad_onset_grace_ms` (3000). |
| `gpu-whisper-cpu-fallback.patch` | `stt/faster_whisper.py` | `local-patch: gpu-fallback` | With `STACKCHAN_FASTER_WHISPER_DEVICE=cuda`, falls back to cpu/int8 if CUDA init or a runtime transcribe fails (GPU full, driver hiccup) instead of killing the STT engine. The mic must never die because the GPU is busy. |
| (earlier) | `stt/faster_whisper.py` | `local-patch: gc-collect-bypass` | 2026-07-16: skip faster_whisper `decode_audio()` (per-call `gc.collect()` cost ~26 s on long-lived process); feed float32 numpy directly. |

Re-apply check after upgrade:

```bash
grep -rn "local-patch:" ~/.local/share/uv/tools/stackchan-mcp/lib/python3.12/site-packages/stackchan_mcp/
```

GPU whisper notes (Devbox2 Blackwell, 2026-07-19):
- `small.en` float16 on cuda = ~1.4 GB VRAM, warm transcribe ~0.13–0.3 s
  (vs ~1–2 s CPU int8). `int8_float16` fails on Blackwell with
  `CUBLAS_STATUS_NOT_SUPPORTED` (ctranslate2 4.8.0) — use float16.
- The whisper model is a small permanent GPU resident inside
  stackchan-gateway.service, NOT a swap tenant: the vision.py guardian
  stops/starts services and never evicts the gateway, so no PEER_SVCS
  registration is needed. If VRAM is ever exhausted at load time the
  cpu fallback patch keeps STT alive.
