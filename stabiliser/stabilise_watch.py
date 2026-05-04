#!/usr/bin/env python3
"""
Watches a staging directory for GoPro footage, stabilizes each clip
via GyroFlow, and moves the result to a date-bucketed processed directory.

Usage:
  python3 stabilize_watch.py
  python3 stabilize_watch.py --once   # process existing files and exit
"""

import argparse
import json
import logging
import re
import shutil
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Config — edit these paths to match your setup
# ---------------------------------------------------------------------------
GYROFLOW = "/Applications/Gyroflow.app/Contents/MacOS/gyroflow"
FFPROBE  = "/usr/local/bin/ffprobe"
FFMPEG   = "/usr/local/bin/ffmpeg"

BASE_DIR      = Path.home() / "Movies" / "Stabiliser"
STAGING_DIR   = BASE_DIR / "Staging"
PROCESSED_DIR = BASE_DIR / "Processed"
FAILED_DIR    = BASE_DIR / "Failed"
LOG_DIR       = BASE_DIR / "Logs"

# Suffix appended to the stabilized output filename
OUTPUT_SUFFIX = "_stabilized"

# Path to a .gyroflow preset file, or None to let GyroFlow use its defaults.
# Create one by opening a clip in the GUI, tuning settings, then File > Save preset.
PRESET: Path | None = None  # e.g. BASE_DIR / "my_preset.gyroflow"

# GyroFlow output parameters — override codec/bitrate here if needed.
# See: gyroflow --help for the full JSON schema.
# Example: '{"codec": "H.265/HEVC", "bitrate": 150, "use_gpu": true, "audio": true}'
OUT_PARAMS: str | None = None

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mts", ".m2ts"}

# How long file size must be unchanged before we consider the copy complete
FILE_STABLE_SECONDS = 10
SIZE_POLL_INTERVAL  = 2   # seconds between size checks during stability wait
SCAN_INTERVAL       = 5   # seconds between directory scans
# ---------------------------------------------------------------------------


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"stabilize_{datetime.now().strftime('%Y%m%d')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout),
        ],
    )


def ensure_dirs() -> None:
    for d in (STAGING_DIR, PROCESSED_DIR, FAILED_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)


def wait_until_stable(path: Path) -> bool:
    """Poll path until its size has been unchanged for FILE_STABLE_SECONDS.
    Returns False if the file disappears during the wait."""
    last_size = -1
    stable_since: float | None = None

    while True:
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return False

        if size == last_size:
            if stable_since is None:
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= FILE_STABLE_SECONDS:
                return True
        else:
            last_size = size
            stable_since = None

        time.sleep(SIZE_POLL_INTERVAL)


def extract_gpmf_sidecar(src: Path, dest_dir: Path, stem: str) -> None:
    """Extract the GPMF metadata stream from src and save it as a .gpmf sidecar."""
    try:
        probe = subprocess.run(
            [FFPROBE, "-v", "quiet", "-print_format", "json", "-show_streams", str(src)],
            capture_output=True, text=True, timeout=30,
        )
        if probe.returncode != 0:
            return
        streams = json.loads(probe.stdout).get("streams", [])
        stream_idx = None
        for s in streams:
            if s.get("codec_type") != "data":
                continue
            tag = s.get("codec_tag_string", "").lower()
            name = s.get("codec_name", "").lower()
            handler = s.get("tags", {}).get("handler_name", "").lower()
            if "tmcd" in tag:
                continue
            if any(x in (tag + name + handler) for x in ("gopr", "gpmd", "gpmf", "gopro")):
                stream_idx = s["index"]
                break
        if stream_idx is None:
            logging.debug(f"[GPMF]   No metadata stream found in {src.name}")
            return

        result = subprocess.run(
            [FFMPEG, "-v", "quiet", "-i", str(src),
             "-map", f"0:{stream_idx}", "-c", "copy", "-f", "rawvideo", "pipe:1"],
            capture_output=True, timeout=60,
        )
        if result.returncode != 0 or not result.stdout:
            return

        sidecar = dest_dir / (stem + ".gpmf")
        sidecar.write_bytes(result.stdout)
        logging.info(f"[GPMF]   Saved motion sidecar → {sidecar.name} ({len(result.stdout):,} bytes)")
    except Exception as exc:
        logging.warning(f"[GPMF]   Could not extract motion sidecar for {src.name}: {exc}")


def build_command(src: Path) -> list[str]:
    cmd = [
        "caffeinate", "-i",  # prevent idle sleep for the duration of the render
        GYROFLOW,
        str(src),
        "--suffix", OUTPUT_SUFFIX,
        "--overwrite",
        "--stdout-progress",
    ]
    if PRESET:
        cmd += ["--preset", str(PRESET)]
    if OUT_PARAMS:
        cmd += ["--out-params", OUT_PARAMS]
    return cmd


def _date_from_spotlight(path: Path) -> date | None:
    try:
        result = subprocess.run(
            ["mdls", "-name", "kMDItemRecordingDate", "-name", "kMDItemContentCreationDate", str(path)],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            if "=" in line and "(null)" not in line:
                match = re.search(r"(\d{4}-\d{2}-\d{2})", line)
                if match:
                    return date.fromisoformat(match.group(1))
    except Exception:
        pass
    return None


def _date_from_birthtime(path: Path) -> date | None:
    try:
        return date.fromtimestamp(path.stat().st_birthtime)
    except AttributeError:
        return None


def _date_from_mtime(path: Path) -> date:
    return date.fromtimestamp(path.stat().st_mtime)


def get_capture_date(path: Path) -> date:
    """Return the recording date from MP4 metadata, falling back to filesystem dates."""
    return (
        _date_from_spotlight(path)
        or _date_from_birthtime(path)
        or _date_from_mtime(path)
    )


def _run_gyroflow(src: Path) -> subprocess.CompletedProcess:
    cmd = build_command(src)
    logging.info(f"[CMD]    {' '.join(cmd)}")
    return subprocess.run(cmd, capture_output=True, text=True, timeout=7200)


def _move_output(expected_output: Path, src: Path) -> Path | None:
    """Move stabilised output to date-bucketed processed dir. Returns destination or None on failure."""
    capture_date = get_capture_date(src)
    date_dir = PROCESSED_DIR / capture_date.isoformat()
    date_dir.mkdir(parents=True, exist_ok=True)
    dest = date_dir / expected_output.name
    shutil.move(str(expected_output), dest)
    logging.info(f"[DONE]   {dest}")
    return dest


def process_file(src: Path) -> None:
    logging.info(f"[START]  {src.name}")
    expected_output = src.with_name(src.stem + OUTPUT_SUFFIX + src.suffix)

    try:
        result = _run_gyroflow(src)
    except subprocess.TimeoutExpired:
        logging.error(f"[TIMEOUT] {src.name} — moving to failed/")
        shutil.move(str(src), FAILED_DIR / src.name)
        return
    except Exception as exc:
        logging.error(f"[ERROR]  {src.name} — {exc}")
        shutil.move(str(src), FAILED_DIR / src.name)
        return

    if result.stdout:
        for line in result.stdout.strip().splitlines():
            logging.info(f"[GFLOW]  {line}")
    if result.stderr:
        for line in result.stderr.strip().splitlines():
            logging.warning(f"[GFLOW]  {line}")

    if result.returncode != 0 or not expected_output.exists():
        logging.error(
            f"[FAIL]   {src.name} — exit code {result.returncode}, "
            f"output {'missing' if not expected_output.exists() else 'present'}"
        )
        shutil.move(str(src), FAILED_DIR / src.name)
        return

    dest = _move_output(expected_output, src)

    extract_gpmf_sidecar(src, dest.parent, dest.stem)

    try:
        src.unlink()
    except OSError as exc:
        logging.warning(f"[WARN]   Could not remove staging file {src.name}: {exc}")


def scan_once(already_seen: set[Path]) -> set[Path]:
    newly_seen: set[Path] = set()
    for path in sorted(STAGING_DIR.iterdir()):
        if path.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        if path in already_seen:
            continue
        newly_seen.add(path)
        logging.info(f"[DETECT] {path.name} — waiting for copy to complete …")
        if wait_until_stable(path):
            process_file(path)
        else:
            logging.warning(f"[GONE]   {path.name} disappeared before processing")
            newly_seen.discard(path)
    return newly_seen


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--once",
        action="store_true",
        help="process any files already in staging, then exit",
    )
    args = parser.parse_args()

    setup_logging()
    ensure_dirs()

    logging.info("=" * 60)
    logging.info(f"Gyroflow : {GYROFLOW}")
    logging.info(f"Staging  : {STAGING_DIR}")
    logging.info(f"Processed: {PROCESSED_DIR}")
    logging.info(f"Failed   : {FAILED_DIR}")
    logging.info(f"Preset   : {PRESET or 'GyroFlow defaults'}")
    logging.info("=" * 60)

    if args.once:
        logging.info("--once mode: processing existing files and exiting")
        scan_once(set())
        return

    logging.info(f"Watching {STAGING_DIR} (Ctrl-C to stop) …")
    seen: set[Path] = set()
    try:
        while True:
            seen |= scan_once(seen)
            time.sleep(SCAN_INTERVAL)
    except KeyboardInterrupt:
        logging.info("Stopped.")


if __name__ == "__main__":
    main()
