# Stabiliser

Watches a staging directory for GoPro footage, stabilises each clip via GyroFlow, and saves the output organised by capture date.

## How it works

1. Copy footage from your SD card into `~/Movies/GoPro-Utils/Stabiliser/Staging/`
2. The watcher detects the new files, waits for the copy to finish, then passes each clip to GyroFlow
3. Source metadata tags (`creation_time`, `firmware`, etc.) are patched back onto the stabilised output — GyroFlow resets them to the processing time during rendering
4. Stabilised output is saved to `~/Movies/GoPro-Utils/Stabiliser/Processed/YYYY-MM-DD/` using the recording date embedded in the original footage
5. The original is deleted from Staging on success, or moved to `Failed/` if something goes wrong

## Directory structure

```
~/Movies/GoPro-Utils/Stabiliser/
  Staging/          ← drop footage here
  Processed/
    2026-05-02/     ← date from the clip's metadata
    2026-05-03/
  Failed/           ← clips that errored during processing
  Logs/
    stabilise_20260503.log   ← structured log, one file per day
    launchd.error.log        ← stderr capture for crash diagnostics
```

All directories are created automatically on first run.

## Requirements

- macOS
- [Homebrew](https://brew.sh) — macOS package manager
- GyroFlow 1.6.3+
- Python 3.13+

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
echo 'export PATH="$(brew --prefix python@3.13)/libexec/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

Verify:

```bash
python3 --version  # Python 3.13.x
```

### 3. Install GyroFlow

```bash
brew install --cask gyroflow
```

This installs GyroFlow to `/Applications/Gyroflow.app`.

### 4. Install the LaunchAgent

```bash
bash ~/Dev/gopro-utils/stabiliser/install.sh
```

The script detects your Python 3.13 path and repo location, writes the plist to `~/Library/LaunchAgents/`, and starts the watcher immediately. It also runs automatically on every login from this point.

To reinstall after moving the repo or upgrading Python, just run it again — it unloads the old agent before reloading.

## Running

The watcher runs automatically at login via a LaunchAgent. No action needed after setup.

To run it manually:

```bash
# Watch continuously (Ctrl-C to stop)
python3 ~/Dev/gopro-utils/stabiliser/stabilise_watch.py

# Process files already in Staging and exit
python3 ~/Dev/gopro-utils/stabiliser/stabilise_watch.py --once
```

## LaunchAgent

The watcher is registered as a LaunchAgent — macOS's built-in mechanism for running background processes. It starts automatically at login and restarts itself if it crashes.

The plist file lives at:

```
~/Library/LaunchAgents/com.$(whoami).stabiliser.plist
```

It tells launchd three things: what to run, to start it at login (`RunAtLoad`), and to keep it alive if it exits (`KeepAlive`). Structured logs go to the dated `stabilise_YYYYMMDD.log` files; stderr is captured to `launchd.error.log` for crash diagnostics. Log files older than 14 days are pruned automatically at startup.

### Installing or reinstalling

Re-run the install script — it handles unload/reload automatically:

```bash
bash ~/Dev/gopro-utils/stabiliser/install.sh
```

### Day-to-day commands

```bash
# Check it's running (shows PID and last exit code)
launchctl list | grep stabiliser

# Stop the watcher
launchctl unload ~/Library/LaunchAgents/com.$(whoami).stabiliser.plist

# Start the watcher
launchctl load ~/Library/LaunchAgents/com.$(whoami).stabiliser.plist

# Watch today's log live
tail -f ~/Movies/GoPro-Utils/Stabiliser/Logs/stabilise_$(date +%Y%m%d).log

# Watch crash/error log
tail -f ~/Movies/GoPro-Utils/Stabiliser/Logs/launchd.error.log
```

### Reading the status output

`launchctl list | grep stabiliser` returns three columns:

```
30184   0   com.$(whoami).stabiliser
```

| Column | Meaning |
|---|---|
| `30184` | PID — process is running |
| `-` | PID — process is not currently running |
| `0` | Last exit code — clean exit or still running |
| non-zero | Last exit code — crashed or errored |

### Removing the LaunchAgent

To stop it running at login entirely:

```bash
launchctl unload ~/Library/LaunchAgents/com.$(whoami).stabiliser.plist
rm ~/Library/LaunchAgents/com.$(whoami).stabiliser.plist
```

## Configuration

All config is at the top of `stabilise_watch.py`:

| Variable | Default | Description |
|---|---|---|
| `GYROFLOW` | `/Applications/Gyroflow.app/...` | Path to the GyroFlow binary |
| `OUTPUT_SUFFIX` | `_stabilized` | Appended to output filenames |
| `PRESET` | `None` | Path to a `.gyroflow` preset file |
| `OUT_PARAMS` | `{"codec": "H.264/AVC", "use_gpu": true, "audio": true}` | GyroFlow output parameters |
| `RENDERING_DEVICE` | auto | GPU for rendering — auto-detected (`"apple m"` on Apple Silicon, `"intel"` on Intel). Override if you have a discrete AMD or Nvidia GPU. |
| `FILE_STABLE_SECONDS` | `10` | Seconds of no size change = copy complete |
| `LOG_RETENTION_DAYS` | `14` | Days to keep `stabilise_*.log` files before pruning |

### Using a preset

Open a clip in GyroFlow, tune the stabilisation settings to your liking, then go to **File > Save preset**. Save it to `~/Movies/Stabiliser/` and point the script at it:

```python
PRESET = Path.home() / "Movies" / "GoPro-Utils" / "Stabiliser" / "my_preset.gyroflow"
```

### Changing codec or bitrate

```python
OUT_PARAMS = '{"codec": "H.265/HEVC", "bitrate": 80, "use_gpu": true, "audio": true}'
```

H.265 at 80 Mbps gives meaningfully smaller files than the GoPro default with barely perceptible quality difference.

## Sleep prevention

Each render is wrapped with `caffeinate -i`, which prevents the Mac from idle sleeping for the duration of the GyroFlow process. The assertion is released automatically when the render finishes. The watcher itself (when idle, waiting for new files) does not prevent sleep.

## Handling failures

If GyroFlow fails to process a clip, the original is moved to `Failed/` and logged. To retry, move the file back into `Staging/`.
