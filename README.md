# gopro-utils

Local tools for processing and editing GoPro cycling footage on macOS — no subscription, no cloud, no app.

## Why this exists

The **GoPro Hero 4K (2024)** records great footage but the intended workflow has several friction points:

- **No onboard stabilisation.** Unlike the Hero 12/13 with HyperSmooth, the Hero 4K has no in-camera stabilisation. Footage needs post-processing before it's usable.
- **GoPro subscription dependency.** Quik's automatic highlight generation, cloud backup, and desktop editing features all require a paid subscription (~$50/year). The highlight algorithm runs on GoPro's servers, not yours.
- **Cloud latency.** Footage has to be uploaded before Quik can process it. For longer rides this means a significant wait before you can review anything.
- **No tunability.** Quik's highlight selection is a black box. You can't adjust what it considers interesting, weight it toward the moments that matter to you, or understand why it picked what it picked.

These tools replace that workflow with something that runs entirely on your Mac:

| Tool | Replaces |
|---|---|
| [Stabiliser](#stabiliser) | GoPro cloud stabilisation + HyperSmooth |
| [Auto Highlights](#auto-highlights) | GoPro Quik automatic highlights |

The only external service used is the [Anthropic API](https://console.anthropic.com/) for vision scoring in the analyser — and that can be skipped with `--no-vision` if you want fully offline operation.

---

## Stabiliser

**`stabiliser/`** — watches a staging directory for new footage, stabilises each clip via [GyroFlow](https://gyroflow.xyz/) using the embedded gyroscope data, and saves the output organised by capture date.

GoPro embeds raw gyroscope and accelerometer readings in every clip as GPMF telemetry. GyroFlow reads this data and computes the stabilisation transform in post, producing results comparable to in-camera HyperSmooth — without needing a subscription or internet connection.

The watcher runs as a macOS LaunchAgent: drop footage into `~/Movies/Stabiliser/Staging/` and it is automatically stabilised and filed to `~/Movies/Stabiliser/Processed/YYYY-MM-DD/`. It also saves a `.gpmf` sidecar file alongside each stabilised clip before deleting the original, preserving the motion data for the analyser.

→ [Full documentation](stabiliser/README.md)

---

## Auto Highlights

**`highlights/`** — scans a directory of footage, ranks every clip by visual and motion quality, and cuts a highlight reel.

Combines two scoring signals:

- **Motion score** — extracted from the GPMF gyroscope and accelerometer data embedded in the clip (or the `.gpmf` sidecar written by the Stabiliser). Rewards dynamic, varied riding over flat steady-state footage.
- **Vision score** — sampled frames are sent to `claude-opus-4-7` and scored on visual appeal, action intensity, and composition, using the same two-stage quality-gate and interestingness logic that GoPro Quik uses internally.

The combined score ranks every clip and identifies the best moments within each one. The tool then cuts a highlight reel automatically — either as individual segments for iMovie or as a single concatenated file.

→ [Full documentation](highlights/README.md)

---

## Requirements

- macOS
- [Homebrew](https://brew.sh)
- Python 3.13+
- ffmpeg 8.0+
- [GyroFlow](https://gyroflow.xyz/) (Stabiliser only)
- An [Anthropic API key](https://console.anthropic.com/) (Clip Analyser vision scoring only)

See each tool's README for installation instructions.
