# Highlights

Scans a directory of GoPro cycling footage and automatically identifies the most interesting clips — and cuts them into a highlight reel. Combines motion data extracted from the camera's built-in sensors with Claude's vision API to produce a ranked list of clips and highlight the best moments within each one.

## How it works

1. **Probe** — reads duration, resolution, and frame rate from every MP4/MOV in the target directory via ffprobe
2. **Motion profile** — extracts the GoPro GPMF telemetry track (gyroscope + accelerometer) and builds a per-second motion magnitude curve for each clip
3. **Frame sampling** — selects up to 24 frames per clip on a regular time grid, biased toward seconds where the motion profile peaks (so fast sections get denser coverage than slow sections)
4. **Vision scoring** — sends each batch of frames to `claude-opus-4-7` with a cycling-specialist prompt; Claude scores visual appeal, action level, and composition for each frame. The system prompt is cached across batches to keep API costs low
5. **Composite score** — combines the vision score (65%) and motion score (35%) into a single rank for each clip
6. **Music prompt** — Claude Haiku synthesises the top frame descriptions into a Beatoven.ai music prompt and stores it in `report.json` for later use with `--music`
7. **Output** — prints a ranked table to the terminal, saves a JSON report and one or more 30-second highlight clips to `~/Movies/GoPro-Utils/Highlights/YYYY-MM-DD/`, and optionally exports all qualifying moments as individual segment files or mixes in an AI soundtrack

## Requirements

- macOS
- [Homebrew](https://brew.sh) — macOS package manager
- Python 3.13+
- ffmpeg 8.0+
- An [Anthropic API key](https://console.anthropic.com/) (only needed for vision scoring; see `--no-vision` to skip)
- A [Beatoven.ai API key](https://www.beatoven.ai/) (only needed for `--music`)

## Installation

### 1. Install Homebrew

If you don't have Homebrew installed:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

### 2. Install Python 3.13

```bash
brew install python@3.13
```

Add it to your PATH so `python3` resolves to the Homebrew version:

```bash
echo 'export PATH="/usr/local/opt/python@3.13/libexec/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

Verify:

```bash
python3 --version  # Python 3.13.x
```

### 3. Install ffmpeg

```bash
brew install ffmpeg
```

Verify:

```bash
ffmpeg -version  # ffmpeg version 8.x
```

### 4. Install Python dependencies

```bash
pip3 install anthropic numpy --break-system-packages
```

### 5. Set your Anthropic API key

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

Add that line to `~/.zshrc` to make it permanent:

```bash
echo 'export ANTHROPIC_API_KEY="sk-ant-..."' >> ~/.zshrc
```

## Usage

### Basic — analyse a directory

```bash
python3 ~/Dev/gopro-utils/highlights/highlights.py ~/Movies/GoPro/
```

Every run produces outputs in a date-stamped directory under `~/Movies/GoPro-Utils/Highlights/`. Re-running on the same day overwrites previous outputs.

1. A ranked table in the terminal
2. `highlights.mp4` — a 30-second highlight clip built from the best-scoring moments (or `highlights_1.mp4`, `highlights_2.mp4`, … with `--clips N`)
3. `report.json` — a full JSON report with scores, frame-level detail, and a Claude-generated music prompt
4. Individual segment files (if `--segments` is passed) — every qualifying moment, uncapped
5. `highlights_with_music.mp4` + `soundtrack.wav` (if `--music` is passed) — reel with an AI-generated soundtrack mixed in

Terminal output:

```
========================================================================
Rank Clip                                  Duration   Score  Top moments
========================================================================
1    GH010042.MP4                          1:12:34    7.83   18:20(8.4)  45:10(8.1)  1:02:05(7.7)
      → 18:20  Fast technical descent with strong light and sharp corners
      → 45:10  Sprint section, rider low over the bars, good colour contrast
2    GH010039.MP4                          0:48:11    6.91   9:00(7.9)   31:50(7.2)  44:00(6.8)
      → 9:00   Open road, strong morning light, clean composition
...
========================================================================
```

### Show only the top N clips

```bash
python3 ~/Dev/gopro-utils/highlights/highlights.py ~/Movies/GoPro/ --top 5
```

### Copy top clips to an export folder

```bash
python3 ~/Dev/gopro-utils/highlights/highlights.py ~/Movies/GoPro/ --top 3 --copy-to ~/Movies/Best/
```

Copies the original (unmodified) source files.

### Re-run the edit without paying for API calls again

After the first run, `report.json` contains all the scoring data. Use `--from-report` to regenerate the highlight reel from it — useful for tuning `--min-score`, `--highlight-window`, `--transition`, `--max-reel-duration`, or adding a soundtrack without spending API credits:

```bash
# Tighten the threshold and add a crossfade
python3 ~/Dev/gopro-utils/highlights/highlights.py \
  --from-report ~/Movies/GoPro-Utils/Highlights/2026-05-04/report.json \
  --min-score 7.5 \
  --transition fade

# Add a soundtrack to an existing reel
python3 ~/Dev/gopro-utils/highlights/highlights.py \
  --from-report ~/Movies/GoPro-Utils/Highlights/2026-05-04/report.json \
  --music

# Regenerate the music prompt with updated genre preferences, then generate 3 Strava clips
python3 ~/Dev/gopro-utils/highlights/highlights.py \
  --from-report ~/Movies/GoPro-Utils/Highlights/2026-05-04/report.json \
  --regen-music-prompt \
  --clips 3
```

The Claude-generated music prompt is stored in `report.json` and reused automatically — no extra API call.

### Export individual segments for editing

Pass `--segments` to export each highlight moment as its own file alongside the combined reel:

```bash
python3 ~/Dev/gopro-utils/highlights/highlights.py ~/Movies/GoPro/ --segments
```

Files are named by source clip and timestamp range, e.g. `001_GX010308_stabilized_0m33s-0m43s.mp4`, and land in the same output directory as `highlights.mp4` and `report.json`. Import the folder into your editor of choice (iMovie, DaVinci Resolve, Final Cut, etc.) and each file is treated as its own clip — drag transitions between them naturally in the timeline.

The files are lossless stream copies of the original footage.

### Controlling what gets included in the edit

```bash
# Raise the threshold — only the most visually impressive moments
python3 ~/Dev/gopro-utils/highlights/highlights.py ~/Movies/GoPro/ \
  --min-score 7.5

# Wider windows — more context around each moment
python3 ~/Dev/gopro-utils/highlights/highlights.py ~/Movies/GoPro/ \
  --highlight-window 8

# Limit to the best 10 clips before selecting highlights
python3 ~/Dev/gopro-utils/highlights/highlights.py ~/Movies/GoPro/ \
  --top 10
```

| Parameter | Default | Effect |
|---|---|---|
| `--min-score` | `6.5` | Minimum combined frame score (1–10) to include |
| `--max-per-clip` | auto | Override max highlight moments per clip (default: ~1 per 45 seconds, capped at 5) |
| `--highlight-window` | `2` | Seconds either side of each qualifying moment (4s clips) |
| `--max-reel-duration` | `30` | Maximum length per output clip in seconds |
| `--clips` | `1` | Number of output clips — each covers a chronological third/quarter/etc. of the ride |
| `--transition` | `none` | Transition style: `none` (hard cut, lossless), `fade` (crossfade), `fadeblack` |
| `--transition-duration` | `0.5` | Length of each transition in seconds |

Two windows less than 1 second apart are always merged into one segment.

When `--segments` is used, **all** qualifying moments are exported as individual files regardless of the reel cap — so you always have the full set for your editor even if the auto reel trimmed some out.

### Crossfade transitions

Hard cuts (the default) are lossless stream copies. Crossfades re-encode using the Mac hardware H.264 encoder:

```bash
python3 ~/Dev/gopro-utils/highlights/highlights.py ~/Movies/GoPro/ \
  --transition fade --transition-duration 0.5
```

### Overriding the output directory

By default each run writes to `~/Movies/GoPro-Utils/Highlights/YYYY-MM-DD/`, overwriting any outputs from an earlier run the same day. Pass `--output` to direct everything to a specific directory instead:

```bash
python3 ~/Dev/gopro-utils/highlights/highlights.py ~/Movies/GoPro/ \
  --output ~/Movies/ThisRide/
```

`report.json`, `highlights.mp4`, and any segment files all go there.

### Motion-only mode (no API cost)

```bash
python3 ~/Dev/gopro-utils/highlights/highlights.py ~/Movies/GoPro/ --no-vision
```

Skips the vision API entirely. Clips are ranked by motion score alone — useful for a quick first pass or testing without spending API credits.

Note: highlight selection in this mode is unreliable because all frame scores are neutral (5.0). Either pass `--min-score 0` to include everything, or run without `--no-vision` for meaningful highlight selection.

### Verbose output

```bash
python3 ~/Dev/gopro-utils/highlights/highlights.py ~/Movies/GoPro/ -v
```

Shows per-clip debug detail: GPMF parse results, frame extraction progress, API call counts.

### All options

```
usage: highlights.py [-h] [--top N] [--chronological] [--copy-to DIR]
                        [--no-vision] [--api-key API_KEY] [--output DIR]
                        [--from-report FILE] [--segments]
                        [--min-score N] [--highlight-window SECS]
                        [--max-reel-duration SECS] [--clips N]
                        [--transition STYLE] [--transition-duration SECS]
                        [--music] [--music-prompt TEXT] [--music-api-key KEY]
                        [--music-volume LEVEL] [--regen-music-prompt]
                        [--verbose]
                        directory

positional arguments:
  directory                 Directory containing GoPro footage (not required with --from-report)

options:
  --top N                   Show only the top N clips (default: all)
  --chronological           Output clips in filming order rather than score order
  --copy-to DIR             Copy top clips to this directory
  --no-vision               Skip the vision API — motion data and neutral scores only
  --api-key API_KEY         Anthropic API key (default: ANTHROPIC_API_KEY env var)
  --output DIR              Output directory (default: ~/Movies/GoPro-Utils/Highlights/YYYY-MM-DD/)
  --prompt-file FILE        Custom scoring prompt (default: prompt.txt alongside the script)
  --from-report FILE        Skip analysis; re-run edit from an existing JSON report
  --segments                Also export each highlight moment as a separate file
  --min-score N             Minimum frame score to include in edit (default: 6.5)
  --max-per-clip N          Maximum highlight moments per clip (default: auto, ~1 per 45 sec)
  --highlight-window SECS   Seconds either side of each highlight moment (default: 2)
  --max-reel-duration SECS  Maximum length per output clip in seconds (default: 30)
  --clips N                 Number of output clips — each covers a chronological portion of the ride (default: 1)
  --transition STYLE        Transition between segments: none, fade, fadeblack (default: none)
  --transition-duration S   Length of each transition in seconds (default: 0.5)
  --music                   Generate and mix an AI soundtrack via Beatoven.ai
  --music-prompt TEXT       Music generation prompt (default: Claude-generated, stored in report.json)
  --music-api-key KEY       Beatoven.ai API key (default: BEATOVEN_API_KEY env var)
  --music-volume LEVEL      Music level in the mix, 0.0–1.0 (default: 0.8)
  --regen-music-prompt      Re-generate the Claude music prompt from existing frame descriptions
  --verbose, -v             Show debug-level detail
```

## Scoring

### Vision score (65% of composite)

Each frame is scored by `claude-opus-4-7` on three dimensions:

| Dimension | Weight | What it scores highly |
|---|---|---|
| Visual | 40% | Dramatic scenery, golden hour or storm light, moorland/mountains, distinctive landmarks — penalises urban roads, flat light, obstructed views |
| Action | 35% | Speed blur on descents, cornering lean, sprint effort, riders passing close, group riding at pace — penalises stationary moments, empty road, junctions |
| Composition | 25% | Leading lines (curving road, stone walls, tree tunnels), another rider as foreground subject, dramatic sky balanced with road — penalises cluttered or flat framing |

The clip's vision score is the highest combined frame score across all sampled frames — it represents the best single moment the model found.

### Motion score (35% of composite)

Built from GPMF accelerometer and gyroscope data embedded in the GoPro file. The score is based on the coefficient of variation (standard deviation ÷ mean) of the per-second motion magnitudes. A clip with consistent, flat motion scores low; one with sharp peaks and dynamic variation scores high.

If no GPMF data is found (e.g. the footage was transcoded and the metadata stream stripped), the motion score falls back to a neutral 5.0 so the vision score still drives the ranking.

### Frame sampling

| Clip length | Sampling interval | Max frames |
|---|---|---|
| Under 10 min | Every 10s | 24 |
| Over 10 min | Every 20s | 24 |

Seconds where the GPMF motion profile exceeds the 80th percentile are also sampled, up to the 24-frame cap. This means a 1-hour clip produces roughly the same number of API calls as a 5-minute clip, with coverage biased toward the most dynamic sections.

## API cost

Vision scoring uses `claude-opus-4-7`. Each API call contains 4 frames (resized to max 1280px) plus the system prompt. The system prompt is cache-controlled, so it is charged at the cached-read rate on every call after the first within a session.

Each API call sends 4 frames at up to 1280px. Image tokens are the dominant cost — roughly 1,200 tokens per frame, so ~4,800 input tokens per call plus a small output payload. At claude-opus-4-7 pricing ($5/1M input, $25/1M output) that works out to roughly **$0.025–0.03 per call**.

| Clips | Avg duration | Approx. API calls | Approx. cost |
|---|---|---|---|
| 5 | 5 min | ~20 | ~$0.50 |
| 10 | 5 min | ~40 | ~$1.00 |
| 20 | 5 min | ~80 | ~$2.00 |

These are estimates based on the token pricing above — check your [Anthropic console](https://console.anthropic.com/) after a real run to calibrate against your actual footage.

Use `--no-vision` to preview results before committing to a full run. Once you have a `report.json`, use `--from-report` to re-run the edit with different settings at no additional cost.

## GPMF telemetry

GoPro cameras embed sensor data in a proprietary binary format called GPMF (GoPro Metadata Format) inside the MP4 container as a separate data stream. This tool extracts and parses that stream directly using ffmpeg, with no third-party GPMF library required.

The parser reads two sensor streams:

- **ACCL** — three-axis accelerometer (m/s²), sampled at ~200 Hz
- **GYRO** — three-axis gyroscope (rad/s), sampled at ~200 Hz

The raw int16 samples are divided by the per-stream `SCAL` factor to produce physical units, then the vector magnitude is computed per sample and averaged per second.

If a `.gpmf` sidecar file exists alongside a clip (written by the Stabiliser tool before it deletes the original), it is used in preference to the embedded stream. This is how motion data is preserved when footage has been stabilised with GyroFlow, which strips the GPMF stream during rendering.

If neither the embedded stream nor a sidecar is found (e.g. footage stabilised externally in iMovie or DaVinci Resolve), the tool falls back gracefully: frame sampling uses a regular grid only, and the motion score is set to neutral.

## Configuration

All tuning constants are at the top of `highlights.py`:

| Variable | Default | Description |
|---|---|---|
| `DEFAULT_OUTPUT_BASE` | `~/Movies/GoPro-Utils/Highlights` | Root directory for date-stamped run outputs |
| `REPORT_FILENAME` | `report.json` | Name of the JSON report within the output directory |
| `REEL_FILENAME` | `highlights.mp4` | Name of the highlight reel within the output directory |
| `MODEL` | `claude-opus-4-7` | Anthropic model for vision scoring |
| `MAX_FRAMES_PER_CLIP` | `24` | Maximum frames sampled per clip |
| `FRAMES_PER_BATCH` | `4` | Frames per API call |
| `FRAME_JPEG_QUALITY` | `4` | JPEG quality for extracted frames (1–31, lower = higher quality) |
| `FRAME_MAX_DIM` | `1280` | Maximum frame dimension sent to the API |
| `W_VISION` | `0.65` | Vision score weight in composite |
| `W_MOTION` | `0.35` | Motion score weight in composite |
| `HIGHLIGHT_HALF_WIDTH` | `2.0` | Seconds either side of each qualifying moment (4s clips) |
| `HIGHLIGHT_MERGE_GAP` | `1.0` | Merge highlight windows within this many seconds of each other |
| `MIN_HIGHLIGHT_SCORE` | `6.5` | Default minimum frame score for highlight selection |
| `MAX_REEL_DURATION` | `30.0` | Maximum length per output clip in seconds |
| `_auto_moments_cap()` | — | Controls the per-clip moment cap formula (~1 per 45 sec, cap 5); edit the function body to change the scaling, or pass `--max-per-clip` to override at runtime |

### Tuning the scoring prompt

The vision scoring criteria live in `highlights/prompt.txt` — plain text, edit it in any editor. Changes take effect immediately on the next run; no Python required.

To experiment with a different prompt without overwriting the default:

```bash
cp ~/Dev/gopro-utils/highlights/prompt.txt ~/Movies/my_prompt.txt
# edit ~/Movies/my_prompt.txt ...
python3 ~/Dev/gopro-utils/highlights/highlights.py ~/Movies/GoPro/ \
  --prompt-file ~/Movies/my_prompt.txt
```

### Adjusting the scoring balance

If you want ranking driven purely by on-bike dynamics (e.g. ranking training sessions by intensity):

```python
W_VISION = 0.30
W_MOTION = 0.70
```

If you want purely aesthetic ranking (e.g. picking cinematic shots regardless of speed):

```python
W_VISION = 1.00
W_MOTION = 0.00
```

### Reducing API cost

Halve the number of API calls by increasing the batch size:

```python
FRAMES_PER_BATCH = 8
```

Or reduce frames per clip:

```python
MAX_FRAMES_PER_CLIP = 12
```

Either way, the system prompt cache still applies across all batches in a single run.

## JSON report format

```json
{
  "music_prompt": "dramatic cycling music, stormy moorland descent, driving rhythm, triumphant",
  "clips": [
    {
      "clip": "~/Movies/GoPro/GH010042.MP4",
      "duration_seconds": 4354.2,
      "composite_score": 7.83,
      "vision_score": 8.41,
      "motion_score": 6.72,
      "best_moments": [
        {
          "timestamp": 1100.0,
          "timestamp_formatted": "18:20",
          "visual": 9,
          "action": 8,
          "composition": 8,
          "combined": 8.4,
          "description": "Fast technical descent with strong light and sharp corners"
        }
      ],
      "frames": [
        { "timestamp": 10.0, "timestamp_formatted": "0:10", "visual": 6, "action": 5, "composition": 6, "combined": 5.75, "description": "..." },
        ...
      ]
    }
  ]
}
```

`music_prompt` is generated by Claude Haiku at the end of each analysis run, synthesised from the top frame descriptions. It is used automatically when `--music` is passed — including on `--from-report` runs. Old reports (bare JSON array) are still supported.

`best_moments` contains the top 5 frames by combined score. `frames` contains every sampled frame, which is what `--from-report` uses to re-run highlight selection with different thresholds.

## Soundtrack

Pass `--music` to generate an AI soundtrack and mix it onto the highlight reel. Uses [Beatoven.ai](https://www.beatoven.ai/) — an API that generates music to an exact duration from a text prompt.

```bash
python3 ~/Dev/gopro-utils/highlights/highlights.py ~/Movies/GoPro/ --music
```

The music prompt is generated by Claude Haiku at the end of each analysis run — synthesised from the top frame descriptions and stored in `report.json`. When you pass `--music`, it uses that stored prompt automatically, including on `--from-report` runs. Override it with `--music-prompt`:

```bash
python3 ~/Dev/gopro-utils/highlights/highlights.py ~/Movies/GoPro/ \
  --music --music-prompt "dramatic orchestral cycling climax"
```

Output: `highlights_with_music.mp4` alongside the existing `highlights.mp4`. The intermediate `soundtrack.wav` is also kept so you can use `--from-report` to remix without regenerating.

`--music` applies to a single clip only. If you use `--clips N`, music generation is skipped with a warning — generate your clips first, then pick the one you want to score separately.

| Flag | Default | Effect |
|---|---|---|
| `--music` | off | Enable soundtrack generation |
| `--music-prompt TEXT` | auto | Override the Claude-generated mood prompt |
| `--music-api-key KEY` | env | Beatoven.ai API key (default: `BEATOVEN_API_KEY` env var) |
| `--music-volume` | `0.8` | Music level in the mix (original audio ducked to 0.15) |

### Setting up Beatoven.ai

1. Create an account at [beatoven.ai](https://www.beatoven.ai/) and obtain an API key from your account settings.
2. Set the key as an environment variable:

```bash
export BEATOVEN_API_KEY="your-key-here"
```

Add that to `~/.zshrc` to make it permanent. No additional Python packages are required — the soundtrack feature uses only the standard library.

## Limitations

- **No GPS** — the GoPro Hero 4K (2024) does not include GPS, so speed and location data are not used. Ranking is based on accelerometer/gyroscope data and visual scoring only.
- **GPMF requires original files or sidecars** — GyroFlow strips the metadata stream when it renders. The Stabiliser tool handles this by writing a `.gpmf` sidecar file alongside each stabilised clip. If you are analysing footage that was stabilised externally (iMovie, DaVinci Resolve, etc.), motion scores will fall back to neutral (5.0).
- **Long clips take time** — ffmpeg frame extraction is fast, but API calls take a second or two each. A 20-clip batch at 6 API calls per clip takes roughly 2–3 minutes.
- **Single directory** — the tool scans one flat directory. It does not recurse into subdirectories.
- **Activity-specific prompt** — the default `prompt.txt` is tuned for cycling footage. It will produce reasonable results for other action-camera activities, but scores will be less meaningful. Use `--prompt-file` to supply a prompt written for your activity.
