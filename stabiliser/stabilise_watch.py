#!/usr/bin/env python3
"""
Watches a staging directory for GoPro footage, stabilizes each clip
via GyroFlow, and moves the result to a date-bucketed processed directory.

Usage:
  python3 stabilise_watch.py
  python3 stabilise_watch.py --once                        # process existing files and exit
  python3 stabilise_watch.py --base-dir ~/Movies/MyRides   # custom base directory
"""

import argparse
import json
import logging
import re
import shutil
import platform
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import NamedTuple

# ---------------------------------------------------------------------------
# Config — edit these to match your setup
# ---------------------------------------------------------------------------
GYROFLOW = "/Applications/Gyroflow.app/Contents/MacOS/gyroflow"
FFPROBE  = "/usr/local/bin/ffprobe"
FFMPEG   = "/usr/local/bin/ffmpeg"

DEFAULT_BASE_DIR = Path.home() / "Movies" / "GoPro-Utils" / "Stabiliser"

# Suffix appended to the stabilized output filename
OUTPUT_SUFFIX = "_stabilized"

# Path to a .gyroflow preset file, or None to let GyroFlow use its defaults.
# Create one by opening a clip in the GUI, tuning settings, then File > Save preset.
PRESET: Path | None = None  # e.g. Path.home() / "Movies" / "my_preset.gyroflow"

# GyroFlow output parameters — override codec/bitrate here if needed.
# See: gyroflow --help for the full JSON schema.
OUT_PARAMS: str | None = '{"codec": "H.264/AVC", "use_gpu": true, "audio": true}'

# GPU rendering device passed to gyroflow -r. Auto-detected from architecture.
# Override if you have a discrete AMD or Nvidia GPU: "amd", "nvidia", "intel", "apple m"
RENDERING_DEVICE: str | None = "apple m" if platform.machine() == "arm64" else "intel"

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mts", ".m2ts"}

# How long file size must be unchanged before we consider the copy complete
FILE_STABLE_SECONDS = 10
SIZE_POLL_INTERVAL  = 2   # seconds between size checks during stability wait
SCAN_INTERVAL       = 5   # seconds between directory scans

# Log files older than this are deleted at startup
LOG_RETENTION_DAYS = 14
# ---------------------------------------------------------------------------


class Dirs(NamedTuple):
    staging: Path
    processed: Path
    failed: Path
    logs: Path


def make_dirs(base: Path) -> Dirs:
    return Dirs(
        staging=base / "Staging",
        processed=base / "Processed",
        failed=base / "Failed",
        logs=base / "Logs",
    )


def _prune_logs(log_dir: Path, keep_days: int = LOG_RETENTION_DAYS) -> None:
    cutoff = datetime.now().timestamp() - keep_days * 86400
    for f in log_dir.glob("stabilise_*.log"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
            logging.info(f"[LOGS]   Pruned old log: {f.name}")


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    _prune_logs(log_dir)
    log_file = log_dir / f"stabilise_{datetime.now().strftime('%Y%m%d')}.log"
    handlers: list[logging.Handler] = [logging.FileHandler(log_file)]
    if sys.stdout.isatty():
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        handlers=handlers,
    )


def ensure_dirs(dirs: Dirs) -> None:
    for d in dirs:
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
    if RENDERING_DEVICE:
        cmd += ["-r", RENDERING_DEVICE]
    return cmd


def _date_from_mp4_metadata(path: Path) -> date | None:
    """Read creation_time from the MP4 container — embedded by the camera at record time."""
    try:
        result = subprocess.run(
            [FFPROBE, "-v", "quiet", "-print_format", "json", "-show_format", str(path)],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            return None
        fmt = json.loads(result.stdout).get("format", {})
        creation_time = fmt.get("tags", {}).get("creation_time", "")
        if creation_time:
            return date.fromisoformat(creation_time[:10])
    except Exception:
        pass
    return None


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
        _date_from_mp4_metadata(path)
        or _date_from_spotlight(path)
        or _date_from_birthtime(path)
        or _date_from_mtime(path)
    )


def _read_format_tags(path: Path) -> dict[str, str]:
    """Return all format-level metadata tags from the MP4 container."""
    try:
        result = subprocess.run(
            [FFPROBE, "-v", "quiet", "-print_format", "json", "-show_format", str(path)],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            return json.loads(result.stdout).get("format", {}).get("tags", {})
    except Exception:
        pass
    return {}


def _patch_metadata(path: Path, tags: dict[str, str]) -> None:
    """Re-write format tags on a file using stream copy (no re-encode)."""
    if not tags:
        return
    tmp = path.with_suffix(".meta_tmp" + path.suffix)
    try:
        metadata_args: list[str] = []
        for key, value in tags.items():
            metadata_args += ["-metadata", f"{key}={value}"]
        result = subprocess.run(
            [FFMPEG, "-v", "quiet", "-i", str(path),
             "-c", "copy"] + metadata_args + ["-y", str(tmp)],
            capture_output=True, timeout=120,
        )
        if result.returncode == 0 and tmp.exists():
            tmp.replace(path)
            logging.info(f"[META]   Restored {len(tags)} tag(s) from source → {path.name}")
        else:
            logging.warning(f"[META]   Could not patch metadata in {path.name}")
    except Exception as exc:
        logging.warning(f"[META]   Metadata patch failed for {path.name}: {exc}")
    finally:
        if tmp.exists():
            tmp.unlink()


def _run_gyroflow(src: Path) -> subprocess.CompletedProcess:
    cmd = build_command(src)
    logging.info(f"[CMD]    {' '.join(cmd)}")
    return subprocess.run(cmd, capture_output=True, text=True, timeout=7200)


def _move_output(expected_output: Path, src: Path, processed_dir: Path) -> Path:
    """Move stabilised output to date-bucketed processed dir. Returns destination."""
    capture_date = get_capture_date(src)
    date_dir = processed_dir / capture_date.isoformat()
    date_dir.mkdir(parents=True, exist_ok=True)
    dest = date_dir / expected_output.name
    shutil.move(str(expected_output), dest)
    logging.info(f"[DONE]   {dest}")
    return dest


def process_file(src: Path, dirs: Dirs) -> None:
    logging.info(f"[START]  {src.name}")
    expected_output = src.with_name(src.stem + OUTPUT_SUFFIX + src.suffix)
    source_tags = _read_format_tags(src)

    try:
        result = _run_gyroflow(src)
    except subprocess.TimeoutExpired:
        logging.error(f"[TIMEOUT] {src.name} — moving to failed/")
        shutil.move(str(src), dirs.failed / src.name)
        return
    except Exception as exc:
        logging.error(f"[ERROR]  {src.name} — {exc}")
        shutil.move(str(src), dirs.failed / src.name)
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
        shutil.move(str(src), dirs.failed / src.name)
        return

    if source_tags:
        _patch_metadata(expected_output, source_tags)

    dest = _move_output(expected_output, src, dirs.processed)
    extract_gpmf_sidecar(src, dest.parent, dest.stem)

    try:
        src.unlink()
    except OSError as exc:
        logging.warning(f"[WARN]   Could not remove staging file {src.name}: {exc}")


def scan_once(already_seen: set[Path], dirs: Dirs) -> set[Path]:
    newly_seen: set[Path] = set()
    for path in sorted(dirs.staging.iterdir()):
        if path.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        if path in already_seen:
            continue
        newly_seen.add(path)
        logging.info(f"[DETECT] {path.name} — waiting for copy to complete …")
        if wait_until_stable(path):
            process_file(path, dirs)
        else:
            logging.warning(f"[GONE]   {path.name} disappeared before processing")
            newly_seen.discard(path)
    return newly_seen


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--base-dir",
        type=Path,
        default=DEFAULT_BASE_DIR,
        metavar="DIR",
        help=f"Base directory for all subdirectories (default: {DEFAULT_BASE_DIR})",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="process any files already in staging, then exit",
    )
    args = parser.parse_args()

    dirs = make_dirs(args.base_dir)

    setup_logging(dirs.logs)
    ensure_dirs(dirs)

    logging.info("=" * 60)
    logging.info(f"Gyroflow : {GYROFLOW}")
    logging.info(f"Base     : {args.base_dir}")
    logging.info(f"Staging  : {dirs.staging}")
    logging.info(f"Processed: {dirs.processed}")
    logging.info(f"Failed   : {dirs.failed}")
    logging.info(f"Preset   : {PRESET or 'GyroFlow defaults'}")
    logging.info("=" * 60)

    if args.once:
        logging.info("--once mode: processing existing files and exiting")
        scan_once(set(), dirs)
        return

    logging.info(f"Watching {dirs.staging} (Ctrl-C to stop) …")
    seen: set[Path] = set()
    try:
        while True:
            seen |= scan_once(seen, dirs)
            time.sleep(SCAN_INTERVAL)
    except KeyboardInterrupt:
        logging.info("Stopped.")


if __name__ == "__main__":
    main()
