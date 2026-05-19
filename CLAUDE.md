# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Two standalone macOS Python scripts that replace the GoPro Quik subscription workflow for cycling footage:

- **`stabiliser/stabilise_watch.py`** — watches a staging directory for raw GoPro footage and stabilises each clip via GyroFlow (using embedded GPMF gyroscope telemetry). Runs as a macOS LaunchAgent. Before deleting each source file it saves a `.gpmf` sidecar containing the raw telemetry — this is critical because GyroFlow strips the GPMF stream from rendered output.
- **`highlights/highlights.py`** — ranks clips by a composite score (65% Claude vision, 35% GPMF motion) and cuts highlight reels. Reads `.gpmf` sidecars when present so motion data survives stabilisation.

There is no build system, no package management, and no test suite. All config lives as module-level constants at the top of each script.

## Running the tools

```bash
# Stabiliser — run manually (normally runs as LaunchAgent)
python3 ~/Dev/gopro-utils/stabiliser/stabilise_watch.py
python3 ~/Dev/gopro-utils/stabiliser/stabilise_watch.py --once   # process Staging and exit

# Highlights — typical post-ride workflow
python3.13 ~/Dev/gopro-utils/highlights/highlights.py ~/Movies/GoPro-Utils/Stabiliser/Processed/2026-05-04/ \
  --clips 3 --import-photos

# Re-run edit from saved report (no API cost)
python3.13 ~/Dev/gopro-utils/highlights/highlights.py \
  --from-report ~/Movies/GoPro-Utils/Highlights/2026-05-04/report.json \
  --clips 3 --min-score 7.0

# Motion-only (no API key needed)
python3.13 ~/Dev/gopro-utils/highlights/highlights.py ~/Movies/GoPro/ --no-vision
```

## LaunchAgent management

```bash
launchctl list | grep stabiliser                          # check running / last exit code
launchctl unload ~/Library/LaunchAgents/com.$(whoami).stabiliser.plist
launchctl load  ~/Library/LaunchAgents/com.$(whoami).stabiliser.plist
tail -f ~/Movies/GoPro-Utils/Stabiliser/Logs/stabilise_$(date +%Y%m%d).log
bash ~/Dev/gopro-utils/stabiliser/install.sh             # reinstall after repo move or Python upgrade
```

## Dependencies

- Python 3.13+, ffmpeg 8.0+, GyroFlow 1.6.3+ (Stabiliser only)
- `pip3 install anthropic numpy --break-system-packages`
- `ANTHROPIC_API_KEY` env var (Highlights vision scoring; skippable with `--no-vision`)

## Architecture: data flow between the two tools

```
SD card
  └─▶ Staging/
        └─▶ stabilise_watch.py
              ├─▶ Processed/YYYY-MM-DD/clip_stabilized.mp4   (GyroFlow output)
              ├─▶ Processed/YYYY-MM-DD/clip.gpmf             (telemetry sidecar)
              └─▶ [source deleted]

  Processed/YYYY-MM-DD/
        └─▶ highlights.py
              ├─ reads .gpmf sidecars for motion scoring
              ├─ calls claude-opus-4-7 for vision scoring
              └─▶ Highlights/YYYY-MM-DD/highlights.mp4 + report.json
```

The `.gpmf` sidecar is the coupling point between the two tools. Highlights falls back gracefully (neutral motion score 5.0) if no sidecar or embedded GPMF stream is found.

## Key implementation details

**GPMF parsing** (`highlights.py`) — the GPMF telemetry is a binary format embedded as a separate MP4 data stream. The parser uses ffmpeg to extract it, then manually decodes the GPMF KLV (key-length-value) structure in Python with no third-party library. It reads `ACCL` (accelerometer) and `GYRO` streams, divides raw int16 samples by the per-stream `SCAL` factor, computes vector magnitude per sample, and averages per second.

**Vision scoring** — frames are extracted via ffmpeg, resized to max 1280px, JPEG-encoded, base64'd, and sent 4 at a time to `claude-opus-4-7`. The system prompt is marked `cache_control: {"type": "ephemeral"}` so it is charged at the cached-read rate on every call after the first within a session.

**Metadata preservation** (`stabilise_watch.py`) — GyroFlow resets all MP4 metadata tags (including `creation_time`) to the processing time during rendering. The script reads source metadata with ffprobe before rendering and patches it back with `ffmpeg -metadata` after.

**`--from-report`** — `report.json` stores per-frame scores for every sampled frame. Re-running with `--from-report` skips all analysis and regenerates the edit (with different `--min-score`, `--clips`, `--transition` etc.) at zero API cost. This is the intended workflow for tuning highlight selection.

## Configuration

All tunable constants are module-level at the top of each script — no config files. For `stabiliser_watch.py`: `GYROFLOW`, `OUTPUT_SUFFIX`, `PRESET`, `OUT_PARAMS`, `RENDERING_DEVICE` (auto-detected: `"apple m"` on Apple Silicon, `"intel"` on Intel). For `highlights.py`: `MODEL`, `W_VISION`/`W_MOTION` weights, `FRAMES_PER_BATCH`, `MIN_HIGHLIGHT_SCORE`, `MAX_REEL_DURATION`.

The vision scoring prompt lives in `highlights/prompt.txt` — plain text, edit directly. Pass `--prompt-file` to use an alternate prompt without modifying the default.
