# gopro-utils

Local tools for processing and editing GoPro cycling footage on macOS — no subscription, no cloud, no app.

## Why this exists

The **GoPro Hero 4K (2024)** records great footage but the intended workflow has several friction points:

- **No onboard stabilisation.** Unlike the Hero 12/13 with HyperSmooth, the Hero 4K has no in-camera stabilisation. Footage needs post-processing before it's usable.
- **GoPro subscription dependency.** Quik's automatic highlight generation, cloud backup, and desktop editing features all require a paid subscription (~$50/year). The highlight algorithm runs on GoPro's servers, not yours.
- **Phone storage.** The intended workflow is to connect the camera to your phone over Wi-Fi and import footage into the Quik app, where stabilisation is applied on-device for free. The catch: GoPro footage is large, and importing directly to the phone quickly eats all available storage. Any serious volume of footage makes this impractical without a subscription for cloud offload.
- **Cloud latency.** With a subscription, footage is automatically uploaded to GoPro's servers for highlight generation and cloud backup. For larger batches of footage this means a significant wait before you can review anything.
- **No tunability.** Quik's highlight selection is a black box. You can't adjust what it considers interesting, weight it toward the moments that matter to you, or understand why it picked what it picked.

These tools replace that workflow with something that runs entirely on your Mac:

| Tool | Replaces |
|---|---|
| [Stabiliser](#stabiliser) | Quik on-device stabilisation + HyperSmooth |
| [Highlights](#highlights) | GoPro Quik automatic highlights |

The only external service used is the [Anthropic API](https://console.anthropic.com/) for vision scoring in the highlights tool — and that can be skipped with `--no-vision` if you want fully offline operation.

---

## Stabiliser

**`stabiliser/`** — watches a staging directory for new footage, stabilises each clip via [GyroFlow](https://gyroflow.xyz/) using the embedded gyroscope data, and saves the output organised by capture date.

GoPro embeds raw gyroscope and accelerometer readings in every clip as GPMF telemetry. GyroFlow reads this data and computes the stabilisation transform in post, producing results comparable to in-camera HyperSmooth — without needing a subscription or internet connection.

The watcher runs as a macOS LaunchAgent: drop footage into `~/Movies/GoPro-Utils/Stabiliser/Staging/` and it is automatically stabilised and filed to `~/Movies/GoPro-Utils/Stabiliser/Processed/YYYY-MM-DD/`. It also saves a `.gpmf` sidecar file alongside each stabilised clip before deleting the original, preserving the motion data for subsequent highlights analysis.

→ [Full documentation](stabiliser/README.md)

---

## Highlights

**`highlights/`** — scans a directory of footage, ranks every clip by visual and motion quality, and cuts a highlight reel.

Combines two scoring signals:

- **Motion score** — extracted from the GPMF gyroscope and accelerometer data embedded in the clip (or the `.gpmf` sidecar written by the Stabiliser). Rewards dynamic, varied motion over flat steady-state footage.
- **Vision score** — sampled frames are sent to `claude-opus-4-7` and scored on visual appeal, action intensity, and composition, using a two-stage quality-gate and interestingness approach modelled on how GoPro Quik is known to work.

The combined score ranks every clip and identifies the best moments within each one. The tool then cuts a highlight reel automatically — either as individual segments for iMovie, DaVinci Resolve, or any other editor, or as a single concatenated file. Optionally generates an AI soundtrack using [MusicGen](https://audiocraft.metademolab.com/) (Meta, runs locally), with the music prompt synthesised by Claude from the frame descriptions.

→ [Full documentation](highlights/README.md)

---

## Requirements

- macOS
- [Homebrew](https://brew.sh)
- Python 3.13+
- ffmpeg 8.0+
- [GyroFlow](https://gyroflow.xyz/) (Stabiliser only)
- An [Anthropic API key](https://console.anthropic.com/) (Highlights vision scoring only)
- Python 3.12 + torch + transformers (Highlights `--music` only; see [installation](highlights/README.md#installing-the-soundtrack-dependencies))

See each tool's README for installation instructions.
