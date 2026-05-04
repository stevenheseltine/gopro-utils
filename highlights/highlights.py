#!/usr/bin/env python3
"""
Scans a directory of GoPro cycling footage and identifies the most interesting clips.

Combines GPMF motion data (gyro/accelerometer) with Claude vision scoring to rank
clips and surface the best moments within each one.

Usage:
  python3 highlights.py ~/Movies/Footage/
  python3 highlights.py ~/Movies/Footage/ --top 5
  python3 highlights.py ~/Movies/Footage/ --no-vision      # motion analysis only
  python3 highlights.py ~/Movies/Footage/ --output ~/Movies/MyRun/
  python3 highlights.py ~/Movies/Footage/ --segments
  python3 highlights.py ~/Movies/Footage/ --min-score 7.0 --transition fade
"""

import argparse
import base64
import json
import logging
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

import anthropic
import numpy as np


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_OUTPUT_BASE = Path.home() / "Movies" / "GoPro-Utils" / "Highlights"
REPORT_FILENAME    = "report.json"
REEL_FILENAME      = "highlights.mp4"
DEFAULT_PROMPT_FILE = Path(__file__).parent / "prompt.txt"

MODEL = "claude-opus-4-7"
VIDEO_EXTENSIONS = {".mp4", ".mov"}

# Max frames sampled per clip for vision scoring
MAX_FRAMES_PER_CLIP = 24
# Frames bundled into a single API call (4 images keeps payload manageable)
FRAMES_PER_BATCH = 4
# JPEG quality for extracted frames (1–31, lower = better)
FRAME_JPEG_QUALITY = 4
# Resize frames to this max dimension before sending to the API
FRAME_MAX_DIM = 1280

# Composite score weights (must sum to 1.0)
W_VISION = 0.65
W_MOTION = 0.35

# Edit generation
HIGHLIGHT_HALF_WIDTH    = 5.0   # seconds either side of each qualifying moment
HIGHLIGHT_MERGE_GAP     = 1.0   # merge windows within this many seconds of each other
MIN_HIGHLIGHT_SCORE     = 6.5   # default minimum combined frame score to include
MAX_REEL_DURATION       = 150.0 # cap the assembled reel at this many seconds (2 min 30)

# Seconds kept clear at each end of a clip when selecting sample timestamps
CLIP_EDGE_MARGIN = 3.0

# Soundtrack generation (MusicGen)
MUSIC_FILENAME           = "soundtrack.wav"
MUSIC_REEL_FILENAME      = "highlights_with_music.mp4"
DEFAULT_MUSIC_MODEL      = "small"
DEFAULT_MUSIC_VOLUME     = 0.8    # music level in mix
DEFAULT_ORIG_AUDIO_VOLUME = 0.15  # original clip audio level in mix

# ---------------------------------------------------------------------------


@dataclass
class ClipInfo:
    path: Path
    duration: float   # seconds
    width: int
    height: int
    fps: float


@dataclass
class FrameScore:
    timestamp: float
    visual: float         # 1–10
    action: float         # 1–10
    composition: float    # 1–10
    description: str

    @property
    def combined(self) -> float:
        return self.visual * 0.4 + self.action * 0.35 + self.composition * 0.25


@dataclass
class ClipResult:
    clip: ClipInfo
    motion_scores: np.ndarray | None   # per-second motion magnitude
    frame_scores: list[FrameScore]

    @property
    def vision_score(self) -> float:
        if not self.frame_scores:
            return 0.0
        return max(f.combined for f in self.frame_scores)

    @property
    def motion_score(self) -> float:
        if self.motion_scores is None or len(self.motion_scores) == 0:
            return 5.0
        arr = self.motion_scores
        mean = float(np.mean(arr))
        std = float(np.std(arr))
        cv = std / max(mean, 0.1)   # coefficient of variation: low = smooth, high = dynamic
        return max(1.0, min(10.0, 1.0 + 18.0 * cv))

    @property
    def composite_score(self) -> float:
        return W_VISION * self.vision_score + W_MOTION * self.motion_score

    @property
    def best_moments(self) -> list[FrameScore]:
        return sorted(self.frame_scores, key=lambda f: f.combined, reverse=True)[:5]


class Segment(NamedTuple):
    clip: ClipInfo
    start: float       # seconds from clip start
    duration: float    # seconds
    score: float       # best combined frame score within the window


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


# ---------------------------------------------------------------------------
# Clip probing
# ---------------------------------------------------------------------------

def probe_clip(path: Path) -> ClipInfo | None:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", "-show_format", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)

        video = next(
            (s for s in data.get("streams", []) if s.get("codec_type") == "video"),
            None,
        )
        if not video:
            return None

        duration = float(data.get("format", {}).get("duration", 0))
        if duration == 0:
            duration = float(video.get("duration", 0))

        num, den = video.get("r_frame_rate", "30/1").split("/")
        fps = int(num) / max(int(den), 1)

        return ClipInfo(
            path=path,
            duration=duration,
            width=video.get("width", 0),
            height=video.get("height", 0),
            fps=fps,
        )
    except Exception as exc:
        logging.warning(f"Could not probe {path.name}: {exc}")
        return None


# ---------------------------------------------------------------------------
# GPMF extraction
# ---------------------------------------------------------------------------

def _find_gpmf_stream(streams: list[dict]) -> int | None:
    for s in streams:
        if s.get("codec_type") != "data":
            continue
        tag = s.get("codec_tag_string", "").lower()
        name = s.get("codec_name", "").lower()
        handler = s.get("tags", {}).get("handler_name", "").lower()
        combined = tag + name + handler
        if "tmcd" in tag:
            continue
        if any(x in combined for x in ("gopr", "gpmd", "gpmf", "gopro")):
            return s["index"]

    # Fallback: first non-timecode data stream
    for s in streams:
        if s.get("codec_type") == "data":
            if "tmcd" not in s.get("codec_tag_string", "").lower():
                return s["index"]

    return None


def extract_gpmf_binary(path: Path) -> bytes | None:
    """Extract raw GPMF binary — from a .gpmf sidecar if present, otherwise from the video stream."""
    sidecar = path.with_suffix(".gpmf")
    if sidecar.exists():
        logging.debug(f"Using GPMF sidecar {sidecar.name}")
        return sidecar.read_bytes()
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        if probe.returncode != 0:
            return None

        streams = json.loads(probe.stdout).get("streams", [])
        idx = _find_gpmf_stream(streams)
        if idx is None:
            return None

        result = subprocess.run(
            ["ffmpeg", "-v", "quiet", "-i", str(path),
             "-map", f"0:{idx}", "-c", "copy", "-f", "rawvideo", "pipe:1"],
            capture_output=True, timeout=60,
        )
        return result.stdout if result.returncode == 0 and result.stdout else None
    except Exception as exc:
        logging.debug(f"GPMF extraction failed for {path.name}: {exc}")
        return None


# ---------------------------------------------------------------------------
# GPMF parser — minimal subset (ACCL + GYRO)
# ---------------------------------------------------------------------------

def _gpmf_items(data: bytes, offset: int, length: int):
    end = offset + length
    while offset + 8 <= end:
        try:
            key = data[offset:offset+4].decode("ascii", errors="replace")
            type_char = chr(data[offset+4])
            elem_size = data[offset+5]
            repeat = struct.unpack_from(">H", data, offset+6)[0]
        except Exception as exc:
            logging.debug(f"GPMF parse error at offset {offset}: {exc}")
            break
        total = elem_size * repeat
        padded = (total + 3) & ~3
        payload = data[offset+8: offset+8+total]
        yield key, type_char, elem_size, repeat, payload
        offset += 8 + padded


def _parse_strm(strm_data: bytes) -> list[tuple[float, float, float]]:
    samples: list[tuple[float, float, float]] = []
    scale = 1
    items = list(_gpmf_items(strm_data, 0, len(strm_data)))

    for key, tc, elem_size, _, payload in items:
        if key == "SCAL":
            if tc == "s" and len(payload) >= 2:
                scale = struct.unpack_from(">h", payload)[0] or 1
            elif tc == "S" and len(payload) >= 2:
                scale = struct.unpack_from(">H", payload)[0] or 1
            elif tc == "l" and len(payload) >= 4:
                scale = struct.unpack_from(">i", payload)[0] or 1

    for key, tc, elem_size, repeat, payload in items:
        if key in ("ACCL", "GYRO") and tc == "s" and elem_size >= 6:
            for i in range(repeat):
                off = i * elem_size
                if off + 6 <= len(payload):
                    x, y, z = struct.unpack_from(">hhh", payload, off)
                    samples.append((x / scale, y / scale, z / scale))

    return samples


def compute_motion_profile(gpmf_data: bytes, duration: float) -> np.ndarray | None:
    all_samples: list[tuple[float, float, float]] = []

    for key, tc, elem_size, repeat, payload in _gpmf_items(gpmf_data, 0, len(gpmf_data)):
        if key == "DEVC":
            for skey, stc, se, sr, sp in _gpmf_items(payload, 0, len(payload)):
                if skey == "STRM":
                    all_samples.extend(_parse_strm(sp))

    if not all_samples:
        return None

    arr = np.array(all_samples, dtype=np.float32)
    magnitudes = np.linalg.norm(arr, axis=1)

    n_seconds = max(1, int(duration))
    bins = np.array_split(magnitudes, n_seconds)
    profile = np.array([float(np.mean(b)) if len(b) else 0.0 for b in bins], dtype=np.float32)
    return profile


# ---------------------------------------------------------------------------
# Frame sampling
# ---------------------------------------------------------------------------

def select_timestamps(clip: ClipInfo, motion: np.ndarray | None) -> list[float]:
    duration = clip.duration
    margin = CLIP_EDGE_MARGIN

    if duration <= margin * 2 + 5:
        return [duration / 2]

    if duration < 600:
        interval = 10.0
    else:
        interval = 20.0

    # Regular grid
    timestamps: list[float] = []
    t = margin
    while t < duration - margin:
        timestamps.append(t)
        t += interval

    # Add motion-peak timestamps if available
    if motion is not None and len(motion) > 10:
        threshold = float(np.percentile(motion, 80))
        for sec, mag in enumerate(motion):
            if float(mag) >= threshold:
                t_peak = float(sec) + 0.5
                if margin < t_peak < duration - margin:
                    timestamps.append(t_peak)

    timestamps = sorted(set(round(t, 1) for t in timestamps))

    if len(timestamps) > MAX_FRAMES_PER_CLIP:
        if motion is not None:
            def motion_at(t: float) -> float:
                idx = min(int(t), len(motion) - 1)
                return float(motion[idx])
            timestamps = sorted(timestamps, key=motion_at, reverse=True)[:MAX_FRAMES_PER_CLIP]
            timestamps.sort()
        else:
            step = len(timestamps) / MAX_FRAMES_PER_CLIP
            timestamps = [timestamps[int(i * step)] for i in range(MAX_FRAMES_PER_CLIP)]

    return timestamps


def extract_frame(path: Path, timestamp: float, tmpdir: Path) -> Path | None:
    out = tmpdir / f"frame_{timestamp:.1f}.jpg"
    result = subprocess.run(
        [
            "ffmpeg", "-v", "quiet",
            "-ss", str(timestamp),
            "-i", str(path),
            "-vframes", "1",
            "-vf", (
                f"scale='min({FRAME_MAX_DIM},iw)':'min({FRAME_MAX_DIM},ih)'"
                ":force_original_aspect_ratio=decrease"
            ),
            "-q:v", str(FRAME_JPEG_QUALITY),
            "-y", str(out),
        ],
        capture_output=True, timeout=30,
    )
    return out if result.returncode == 0 and out.exists() else None


# ---------------------------------------------------------------------------
# Vision scoring
# ---------------------------------------------------------------------------

def _load_prompt(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        print(f"ERROR: prompt file not found: {path}", file=sys.stderr)
        sys.exit(1)


def _parse_score_response(text: str) -> list[dict]:
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        if text.startswith("json"):
            text = text[4:]
    parsed = json.loads(text)
    if isinstance(parsed, dict):
        parsed = [parsed]
    return parsed


def score_frames_batch(
    client: anthropic.Anthropic,
    batch: list[tuple[float, Path]],
    system_prompt: str,
) -> list[FrameScore]:
    content: list[dict] = []
    for ts, frame_path in batch:
        with open(frame_path, "rb") as f:
            b64 = base64.standard_b64encode(f.read()).decode()
        content.append({"type": "text", "text": f"Frame at {ts:.1f}s:"})
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
        })

    n = len(batch)
    content.append({
        "type": "text",
        "text": (
            f"Score each of the {n} frame(s) above. "
            "Reply with a JSON array, one object per frame in the same order."
        ),
    })

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=512,
            system=[{
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": content}],
        )
        raw = _parse_score_response(response.content[0].text)
        results = []
        for i, (ts, _) in enumerate(batch):
            s = raw[i] if i < len(raw) else {}
            results.append(FrameScore(
                timestamp=ts,
                visual=float(s.get("visual", 5)),
                action=float(s.get("action", 5)),
                composition=float(s.get("composition", 5)),
                description=str(s.get("description", "")),
            ))
        return results
    except Exception as exc:
        logging.warning(f"Vision scoring failed: {exc}")
        return [
            FrameScore(timestamp=ts, visual=5.0, action=5.0, composition=5.0, description="")
            for ts, _ in batch
        ]


# ---------------------------------------------------------------------------
# Per-clip analysis pipeline
# ---------------------------------------------------------------------------

def analyse_clip(
    clip: ClipInfo,
    client: anthropic.Anthropic | None,
    tmpdir: Path,
    system_prompt: str,
) -> ClipResult:
    logging.info(f"[ANALYSE] {clip.path.name}  ({_fmt_duration(clip.duration)})")

    motion_scores: np.ndarray | None = None
    gpmf_data = extract_gpmf_binary(clip.path)
    if gpmf_data:
        motion_scores = compute_motion_profile(gpmf_data, clip.duration)
        if motion_scores is not None:
            logging.info(
                f"  Motion: {len(motion_scores)}s of data, "
                f"peak={float(np.max(motion_scores)):.1f} "
                f"cv={float(np.std(motion_scores)/max(float(np.mean(motion_scores)),0.1)):.2f}"
            )
        else:
            logging.info("  Motion: GPMF stream found but no ACCL/GYRO samples — using neutral score")
    else:
        logging.info("  Motion: no GPMF stream found — using neutral score (5.0)")

    # Select and extract frames
    timestamps = select_timestamps(clip, motion_scores)
    logging.info(f"  Sampling {len(timestamps)} frames")

    frame_pairs: list[tuple[float, Path]] = []
    for ts in timestamps:
        fp = extract_frame(clip.path, ts, tmpdir)
        if fp:
            frame_pairs.append((ts, fp))

    # Vision scoring
    frame_scores: list[FrameScore] = []
    if client is not None and frame_pairs:
        for i in range(0, len(frame_pairs), FRAMES_PER_BATCH):
            batch = frame_pairs[i: i + FRAMES_PER_BATCH]
            scores = score_frames_batch(client, batch, system_prompt)
            frame_scores.extend(scores)
            end_idx = min(i + FRAMES_PER_BATCH, len(frame_pairs))
            logging.info(f"  Scored frames {i+1}–{end_idx}/{len(frame_pairs)}")
    else:
        frame_scores = [
            FrameScore(timestamp=ts, visual=5.0, action=5.0, composition=5.0, description="")
            for ts, _ in frame_pairs
        ]

    return ClipResult(clip=clip, motion_scores=motion_scores, frame_scores=frame_scores)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _fmt_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _fmt_ts(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def print_report(results: list[ClipResult]) -> None:
    print()
    print("=" * 76)
    print(f"{'Rank':<5}{'Clip':<38}{'Duration':<11}{'Score':<7}Top moments")
    print("=" * 76)
    for rank, r in enumerate(results, 1):
        moments = "  ".join(
            f"{_fmt_ts(f.timestamp)}({f.combined:.1f})"
            for f in r.best_moments[:3]
        )
        name = r.clip.path.name
        if len(name) > 37:
            name = name[:34] + "..."
        print(f"{rank:<5}{name:<38}{_fmt_duration(r.clip.duration):<11}{r.composite_score:.2f}   {moments}")
        for f in r.best_moments[:2]:
            if f.description:
                print(f"      → {_fmt_ts(f.timestamp):>5}  {f.description}")
    print("=" * 76)
    print()


def _frame_to_dict(f: FrameScore) -> dict:
    return {
        "timestamp": f.timestamp,
        "timestamp_formatted": _fmt_ts(f.timestamp),
        "visual": f.visual,
        "action": f.action,
        "composition": f.composition,
        "combined": round(f.combined, 2),
        "description": f.description,
    }


def save_json_report(
    results: list[ClipResult],
    output_path: Path,
    music_prompt: str | None = None,
) -> None:
    clips = [
        {
            "clip": str(r.clip.path),
            "duration_seconds": r.clip.duration,
            "composite_score": round(r.composite_score, 3),
            "vision_score": round(r.vision_score, 3),
            "motion_score": round(r.motion_score, 3),
            "frames": [_frame_to_dict(f) for f in r.frame_scores],
            "best_moments": [_frame_to_dict(f) for f in r.best_moments],
        }
        for r in results
    ]
    data: dict = {"clips": clips}
    if music_prompt:
        data["music_prompt"] = music_prompt
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as fh:
        json.dump(data, fh, indent=2)
    logging.info(f"Report saved to {output_path}")


def load_results_from_json(report_path: Path) -> tuple[list[ClipResult], str | None]:
    with open(report_path) as fh:
        data = json.load(fh)

    # Support both old format (bare list) and new format (dict with "clips" key)
    if isinstance(data, list):
        entries = data
        music_prompt = None
    else:
        entries = data.get("clips", [])
        music_prompt = data.get("music_prompt")

    results = []
    for entry in entries:
        clip_path = Path(entry["clip"])
        clip_info = probe_clip(clip_path)
        if clip_info is None:
            clip_info = ClipInfo(
                path=clip_path,
                duration=entry["duration_seconds"],
                width=0, height=0, fps=0,
            )

        raw_frames = entry.get("frames") or entry.get("best_moments", [])
        frame_scores = [
            FrameScore(
                timestamp=f["timestamp"],
                visual=f["visual"],
                action=f["action"],
                composition=f["composition"],
                description=f.get("description", ""),
            )
            for f in raw_frames
        ]

        results.append(ClipResult(
            clip=clip_info,
            motion_scores=None,
            frame_scores=frame_scores,
        ))

    return results, music_prompt


# ---------------------------------------------------------------------------
# Edit generation
# ---------------------------------------------------------------------------

def _auto_moments_cap(duration_seconds: float) -> int:
    """Scale max highlight moments by clip duration: ~1 per 45 seconds, capped at 5."""
    return max(1, min(5, int(duration_seconds / 45) + 1))


def select_edit_segments(
    results: list[ClipResult],
    min_score: float,
    half_width: float,
    merge_gap: float,
    max_per_clip: int,
) -> list[Segment]:
    """
    Expand qualifying moments into windows, merge overlapping or adjacent windows,
    and return all qualifying segments in chronological order.
    max_per_clip=0 uses automatic duration-based scaling.
    """
    all_segments: list[Segment] = []

    for r in results:
        cap = max_per_clip if max_per_clip > 0 else _auto_moments_cap(r.clip.duration)
        qualifying = [f for f in r.best_moments if f.combined >= min_score][:cap]
        if not qualifying:
            continue

        # Build and sort windows by start time; carry the frame score through merges
        windows: list[list] = []  # [start, end, score]
        for f in qualifying:
            start = max(0.0, f.timestamp - half_width)
            end   = min(r.clip.duration, f.timestamp + half_width)
            windows.append([start, end, f.combined])
        windows.sort()

        merged: list[list] = []
        for start, end, score in windows:
            if merged and start <= merged[-1][1] + merge_gap:
                merged[-1][1] = max(merged[-1][1], end)
                merged[-1][2] = max(merged[-1][2], score)
            else:
                merged.append([start, end, score])

        for start, end, score in merged:
            all_segments.append(Segment(r.clip, start, end - start, score))

    all_segments.sort(key=lambda s: (s.clip.path.name, s.start))
    return all_segments


def _cap_reel_segments(segments: list[Segment], max_duration: float) -> list[Segment]:
    """
    Pick the highest-scoring segments that fit within max_duration,
    then re-sort chronologically for natural viewing order.
    """
    ranked = sorted(segments, key=lambda s: s.score, reverse=True)
    selected: list[Segment] = []
    total = 0.0
    for seg in ranked:
        if total + seg.duration <= max_duration:
            selected.append(seg)
            total += seg.duration
    selected.sort(key=lambda s: (s.clip.path.name, s.start))
    return selected


def extract_segment(src: Path, start: float, duration: float, out: Path) -> bool:
    """Cut a segment from src using stream copy. Returns True on success."""
    result = subprocess.run(
        [
            "ffmpeg", "-v", "quiet",
            "-ss", f"{start:.3f}",
            "-i", str(src),
            "-t", f"{duration:.3f}",
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            "-y", str(out),
        ],
        capture_output=True, timeout=120,
    )
    if result.returncode != 0:
        logging.debug(f"segment extraction stderr: {result.stderr.decode()}")
    return result.returncode == 0 and out.exists()


def _segment_duration(path: Path) -> float:
    """Return the actual duration of an extracted segment via ffprobe."""
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(path)],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode == 0:
        return float(json.loads(result.stdout).get("format", {}).get("duration", 0.0))
    return 0.0


class _XfadeGraph(NamedTuple):
    filter_complex: str
    video_map: str
    audio_map: str


def _build_xfade_filtergraph(
    durations: list[float],
    transition: str,
    fade_dur: float,
) -> _XfadeGraph:
    """
    Build an ffmpeg filter_complex string that chains xfade + acrossfade
    across N segments. Returns (filter_complex, video_map, audio_map).
    """
    n = len(durations)
    if n == 1:
        return _XfadeGraph("", "0:v", "0:a")

    v_filters: list[str] = []
    a_filters: list[str] = []
    offset = 0.0

    for i in range(1, n):
        offset += durations[i - 1] - fade_dur
        in_v  = f"[v{i-1}]" if i > 1 else "[0:v]"
        in_a  = f"[a{i-1}]" if i > 1 else "[0:a]"
        out_v = f"[v{i}]"   if i < n - 1 else "[vout]"
        out_a = f"[a{i}]"   if i < n - 1 else "[aout]"

        v_filters.append(
            f"{in_v}[{i}:v]xfade=transition={transition}"
            f":duration={fade_dur}:offset={max(0.0, offset):.3f}{out_v}"
        )
        a_filters.append(
            f"{in_a}[{i}:a]acrossfade=d={fade_dur}{out_a}"
        )

    return _XfadeGraph(";".join(v_filters + a_filters), "[vout]", "[aout]")


def _concat_copy(segment_paths: list[Path], output_path: Path, tmp: Path) -> bool:
    """Concatenate segments with stream copy (fast, no re-encoding)."""
    concat_list = tmp / "concat.txt"
    with open(concat_list, "w") as fh:
        for p in segment_paths:
            fh.write(f"file '{p}'\n")

    result = subprocess.run(
        [
            "ffmpeg", "-v", "error",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_list),
            "-c", "copy",
            "-y", str(output_path),
        ],
        capture_output=True, timeout=600,
    )
    if result.returncode != 0:
        logging.error(f"[EDIT]   Concatenation failed: {result.stderr.decode().strip()}")
    return result.returncode == 0 and output_path.exists()


def _concat_xfade(
    segment_paths: list[Path],
    output_path: Path,
    transition: str,
    fade_dur: float,
) -> bool:
    """Concatenate segments with xfade transitions (requires re-encoding)."""
    durations = [_segment_duration(p) for p in segment_paths]

    # Warn if any segment is too short for the transition
    for i, (_, d) in enumerate(zip(segment_paths, durations)):
        if d <= fade_dur * 2:
            logging.warning(
                f"[EDIT]   Segment {i+1} is {d:.1f}s — shorter than 2× transition "
                f"duration ({fade_dur * 2:.1f}s). Transition may look odd."
            )

    graph = _build_xfade_filtergraph(durations, transition, fade_dur)

    inputs: list[str] = []
    for p in segment_paths:
        inputs += ["-i", str(p)]

    encode = ["-c:v", "h264_videotoolbox", "-b:v", "20M", "-c:a", "aac", "-b:a", "192k", "-y", str(output_path)]

    if graph.filter_complex:
        cmd = (
            ["ffmpeg", "-v", "quiet"] + inputs
            + ["-filter_complex", graph.filter_complex, "-map", graph.video_map, "-map", graph.audio_map]
            + encode
        )
    else:
        cmd = ["ffmpeg", "-v", "quiet"] + inputs + encode

    result = subprocess.run(cmd, capture_output=True, timeout=600)
    if result.returncode != 0:
        logging.error(f"[EDIT]   xfade encode failed: {result.stderr.decode()}")
    return result.returncode == 0 and output_path.exists()


def _segment_filename(seq: int, clip: ClipInfo, start: float, duration: float) -> str:
    """Return a descriptive filename for an exported segment."""
    def ts(s: float) -> str:
        m, sec = divmod(int(s), 60)
        return f"{m}m{sec:02d}s" if m else f"{sec}s"
    return f"{seq:03d}_{clip.path.stem}_{ts(start)}-{ts(start + duration)}.mp4"


def _extract_segments(segments: list[Segment], tmp: Path) -> list[Path]:
    paths = []
    for i, seg in enumerate(segments):
        out = tmp / f"seg_{i:04d}.mp4"
        logging.info(
            f"[EDIT]   {i+1}/{len(segments)}  {seg.clip.path.name}  "
            f"{_fmt_ts(seg.start)} → {_fmt_ts(seg.start + seg.duration)}  ({seg.duration:.1f}s)"
        )
        if extract_segment(seg.clip.path, seg.start, seg.duration, out):
            paths.append(out)
        else:
            logging.warning(f"[EDIT]   Skipping segment {i+1} — extraction failed")
    return paths


def _save_segment_files(
    segment_paths: list[Path],
    segments: list[Segment],
    segments_dir: Path,
) -> None:
    segments_dir.mkdir(parents=True, exist_ok=True)
    for i, (tmp_path, seg) in enumerate(zip(segment_paths, segments), start=1):
        name = _segment_filename(i, seg.clip, seg.start, seg.duration)
        dest = segments_dir / name
        shutil.copy2(tmp_path, dest)
        logging.info(f"[EDIT]   Segment → {dest.name}")
    logging.info(f"[EDIT]   {len(segment_paths)} segment(s) saved to {segments_dir}")


def _assemble_reel(
    segment_paths: list[Path],
    output_path: Path,
    total_duration: float,
    transition: str,
    transition_duration: float,
    tmp: Path,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    logging.info(f"[EDIT]   Assembling → {output_path}")
    if transition == "none":
        ok = _concat_copy(segment_paths, output_path, tmp)
    else:
        ok = _concat_xfade(segment_paths, output_path, transition, transition_duration)
    if ok:
        logging.info(f"[EDIT]   Saved → {output_path}  (~{_fmt_duration(total_duration)})")


def build_edit(
    results: list[ClipResult],
    output_dir: Path,
    save_segments: bool,
    min_score: float,
    half_width: float,
    max_per_clip: int,
    transition: str,
    transition_duration: float,
    max_reel_duration: float,
) -> None:
    all_segments = select_edit_segments(results, min_score, half_width, HIGHLIGHT_MERGE_GAP, max_per_clip)

    if not all_segments:
        logging.error(
            f"[EDIT]   No segments found above score {min_score:.1f}. "
            "Try lowering --min-score, or run without --no-vision."
        )
        return

    reel_segments = _cap_reel_segments(all_segments, max_reel_duration)
    reel_total = sum(s.duration for s in reel_segments)
    mode = f"{transition} ({transition_duration}s)" if transition != "none" else "hard cut"
    logging.info(
        f"[EDIT]   {len(all_segments)} qualifying segment(s) — "
        f"reel: {len(reel_segments)} segment(s), ~{_fmt_duration(reel_total)}  "
        f"transition: {mode}"
    )

    with tempfile.TemporaryDirectory(prefix="highlights_edit_") as tmpdir:
        tmp = Path(tmpdir)
        all_paths = _extract_segments(all_segments, tmp)

        if not all_paths:
            logging.error("[EDIT]   All segment extractions failed")
            return

        if save_segments:
            _save_segment_files(all_paths, all_segments, output_dir)

        # Build a path lookup keyed by (clip path, start time) to map reel segments → extracted files
        path_by_key = {(s.clip.path, s.start): p for s, p in zip(all_segments, all_paths)}
        reel_paths = [path_by_key[(s.clip.path, s.start)] for s in reel_segments]

        _assemble_reel(reel_paths, output_dir / REEL_FILENAME, reel_total, transition, transition_duration, tmp)


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------

def _probe_clips(clip_paths: list[Path]) -> list[ClipInfo]:
    clips = []
    for p in clip_paths:
        info = probe_clip(p)
        if info:
            clips.append(info)
        else:
            logging.warning(f"Skipping {p.name} — could not read metadata")
    return clips


def _make_api_client(api_key: str | None) -> anthropic.Anthropic:
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        logging.error(
            "No Anthropic API key found. "
            "Set ANTHROPIC_API_KEY or pass --api-key. "
            "Use --no-vision to skip vision analysis."
        )
        sys.exit(1)
    return anthropic.Anthropic(api_key=key)


def _copy_clips(results: list[ClipResult], dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    for r in results:
        dest = dest_dir / r.clip.path.name
        logging.info(f"Copying {r.clip.path.name} → {dest}")
        shutil.copy2(r.clip.path, dest)
    logging.info(f"Copied {len(results)} clip(s) to {dest_dir}")


def _run_analysis(
    clips: list[ClipInfo],
    client: anthropic.Anthropic | None,
    system_prompt: str,
) -> list[ClipResult]:
    results = []
    with tempfile.TemporaryDirectory(prefix="highlights_") as tmpdir:
        tmp = Path(tmpdir)
        for clip in clips:
            results.append(analyse_clip(clip, client, tmp, system_prompt))
    return results


# ---------------------------------------------------------------------------
# Soundtrack
# ---------------------------------------------------------------------------

_MUSIC_MODEL_SIZES = {"small": "300 MB", "medium": "1.5 GB", "large": "3.3 GB"}


def _generate_music_prompt_via_claude(
    results: list[ClipResult],
    client: anthropic.Anthropic,
) -> str:
    top_descriptions = sorted(
        [
            (f.combined, f.description)
            for r in results
            for f in r.best_moments[:2]
            if f.description
        ],
        reverse=True,
    )[:6]

    descriptions = "\n".join(
        f"- {desc} ({score:.1f})" for score, desc in top_descriptions
    )
    response = client.messages.create(
        model=MUSIC_PROMPT_MODEL,
        max_tokens=80,
        messages=[{
            "role": "user",
            "content": (
                "Based on these descriptions of the best moments in a GoPro highlight reel, "
                "write a single music generation prompt (under 30 words) for MusicGen. "
                "Output only the prompt, nothing else.\n\n"
                f"{descriptions}"
            ),
        }],
    )
    return response.content[0].text.strip()


def _derive_music_prompt(results: list[ClipResult]) -> str:
    avg_composite = sum(r.composite_score for r in results) / len(results)
    avg_motion    = sum(r.motion_score    for r in results) / len(results)
    if avg_composite >= 7.5 and avg_motion >= 6.5:
        return "upbeat energetic action sports music, driving rhythm, fast-paced, exciting"
    if avg_composite >= 6.5 or avg_motion >= 6.0:
        return "cinematic adventure music, inspiring, moderate tempo, outdoor"
    return "ambient atmospheric music, calm, peaceful, scenic outdoor"


MUSIC_PROMPT_MODEL = "claude-haiku-4-5-20251001"

_MUSIC_GEN_HELPER = Path(__file__).parent / "_music_gen.py"
_MUSIC_PYTHON_CANDIDATES = ["python3.12", "python3.11", "python3"]


def _find_music_python() -> str | None:
    for py in _MUSIC_PYTHON_CANDIDATES:
        result = subprocess.run(
            [py, "-c", "import torch; from transformers import MusicgenForConditionalGeneration"],
            capture_output=True,
        )
        if result.returncode == 0:
            return py
    return None


def _generate_soundtrack(
    duration: float,
    prompt: str,
    model_size: str,
    output_path: Path,
) -> None:
    py = _find_music_python()
    if py is None:
        logging.error(
            "No Python with torch + transformers found. "
            "See highlights/README.md — Soundtrack section for install instructions."
        )
        sys.exit(1)

    size_label = _MUSIC_MODEL_SIZES.get(model_size, "?")
    logging.info(f"MusicGen-{model_size} ({size_label}) via {py}")

    result = subprocess.run(
        [py, str(_MUSIC_GEN_HELPER), str(duration), prompt, model_size, str(output_path)],
    )
    if result.returncode != 0:
        logging.error("Music generation failed")
        sys.exit(1)
    logging.info(f"Soundtrack saved: {output_path.name}")


def _mix_soundtrack(
    video_path: Path,
    audio_path: Path,
    output_path: Path,
    music_vol: float,
    orig_vol: float,
) -> None:
    filter_complex = (
        f"[0:a]volume={orig_vol}[va];"
        f"[1:a]volume={music_vol}[ma];"
        f"[va][ma]amix=inputs=2:duration=first[aout]"
    )
    cmd = [
        "ffmpeg", "-v", "quiet",
        "-i", str(video_path),
        "-i", str(audio_path),
        "-filter_complex", filter_complex,
        "-map", "0:v",
        "-map", "[aout]",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        "-y", str(output_path),
    ]
    result = subprocess.run(cmd)
    if result.returncode != 0:
        logging.error("Audio mix failed")
        sys.exit(1)
    logging.info(f"Mixed reel: {output_path.name}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("directory", type=Path, nargs="?", help="Directory containing GoPro footage (not required with --from-report)")
    parser.add_argument("--top", type=int, default=0, metavar="N",
                        help="Show only the top N clips (default: all)")
    parser.add_argument("--chronological", action="store_true",
                        help="Output clips in filming order rather than score order")
    parser.add_argument("--copy-to", type=Path, metavar="DIR",
                        help="Copy top clips to this directory")
    parser.add_argument("--no-vision", action="store_true",
                        help="Skip the vision API — use motion data and neutral scores only")
    parser.add_argument("--api-key", type=str, default=None,
                        help="Anthropic API key (default: ANTHROPIC_API_KEY env var)")
    parser.add_argument("--output", type=Path, default=None, metavar="DIR",
                        help=f"Output directory for report.json, highlights.mp4, and any segments "
                             f"(default: {DEFAULT_OUTPUT_BASE}/YYYY-MM-DD-HH-MM-SS/)")
    parser.add_argument("--prompt-file", type=Path, default=None, metavar="FILE",
                        help=f"Path to a custom scoring prompt (default: {DEFAULT_PROMPT_FILE.name} in the script directory)")
    parser.add_argument("--from-report", type=Path, default=None, metavar="FILE",
                        help="Skip analysis; re-run edit from an existing JSON report")
    parser.add_argument("--segments", action="store_true",
                        help="Also export each highlight moment as a separate file in the output directory")
    parser.add_argument("--min-score", type=float, default=MIN_HIGHLIGHT_SCORE, metavar="N",
                        help=f"Minimum frame score to include in the edit (default: {MIN_HIGHLIGHT_SCORE})")
    parser.add_argument("--max-per-clip", type=int, default=0, metavar="N",
                        help="Maximum highlight moments per clip (default: auto — ~1 per 2 min, capped at 5)")
    parser.add_argument("--highlight-window", type=float, default=HIGHLIGHT_HALF_WIDTH, metavar="SECS",
                        help=f"Seconds either side of each highlight moment (default: {HIGHLIGHT_HALF_WIDTH})")
    parser.add_argument("--max-reel-duration", type=float, default=MAX_REEL_DURATION, metavar="SECS",
                        help=f"Maximum reel length in seconds — best-scoring segments are picked first "
                             f"(default: {int(MAX_REEL_DURATION)})")
    parser.add_argument("--transition", default="none",
                        choices=["none", "fade", "fadeblack"],
                        help="Transition between clips: none (hard cut, lossless), fade, fadeblack (default: none)")
    parser.add_argument("--transition-duration", type=float, default=0.5, metavar="SECS",
                        help="Duration of each transition in seconds (default: 0.5)")
    music = parser.add_argument_group("soundtrack (requires audiocraft)")
    music.add_argument("--music", action="store_true",
                       help="Generate and mix an AI soundtrack using MusicGen")
    music.add_argument("--music-prompt", type=str, default=None, metavar="TEXT",
                       help="Music generation prompt (default: Claude-generated from frame descriptions, stored in report.json)")
    music.add_argument("--music-model", choices=["small", "medium", "large"],
                       default=DEFAULT_MUSIC_MODEL,
                       help="MusicGen model size: small (~300MB), medium (~1.5GB), large (~3.3GB) (default: small)")
    music.add_argument("--music-volume", type=float, default=DEFAULT_MUSIC_VOLUME, metavar="LEVEL",
                       help=f"Music level in the mix, 0.0–1.0 (default: {DEFAULT_MUSIC_VOLUME})")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)

    system_prompt = _load_prompt(args.prompt_file or DEFAULT_PROMPT_FILE)
    if args.prompt_file:
        logging.info(f"Using custom prompt: {args.prompt_file}")

    run_dir = DEFAULT_OUTPUT_BASE / datetime.now().strftime("%Y-%m-%d-%H-%M-%S")

    if args.from_report:
        if not args.from_report.exists():
            logging.error(f"Report not found: {args.from_report}")
            sys.exit(1)
        logging.info(f"Loading results from {args.from_report}")
        results, stored_music_prompt = load_results_from_json(args.from_report)
    else:
        if not args.directory:
            parser.error("directory is required unless --from-report is specified")
        if not args.directory.is_dir():
            logging.error(f"Not a directory: {args.directory}")
            sys.exit(1)

        clip_paths = sorted(
            p for p in args.directory.iterdir()
            if p.suffix.lower() in VIDEO_EXTENSIONS
        )
        if not clip_paths:
            logging.error(f"No video files found in {args.directory}")
            sys.exit(1)

        logging.info(f"Found {len(clip_paths)} clip(s) in {args.directory}")

        clips = _probe_clips(clip_paths)
        if not clips:
            logging.error("No readable clips found")
            sys.exit(1)

        client: anthropic.Anthropic | None = None
        if not args.no_vision:
            client = _make_api_client(args.api_key)

        results = _run_analysis(clips, client, system_prompt)
        stored_music_prompt = None

    results.sort(key=lambda r: r.composite_score, reverse=True)

    if args.top:
        results = results[: args.top]

    if args.chronological:
        results.sort(key=lambda r: r.clip.path.name)

    print_report(results)

    output_dir = args.output or run_dir

    # Generate music prompt from Claude if we ran a fresh analysis with vision
    if not args.from_report and client is not None and stored_music_prompt is None:
        try:
            stored_music_prompt = _generate_music_prompt_via_claude(results, client)
            logging.info(f"Music prompt: {stored_music_prompt!r}")
        except Exception as exc:
            logging.warning(f"Could not generate music prompt via Claude: {exc}")

    save_json_report(results, output_dir / REPORT_FILENAME, music_prompt=stored_music_prompt)

    if args.copy_to:
        _copy_clips(results, args.copy_to)

    if args.no_vision:
        logging.warning(
            "[EDIT]   --no-vision was used so all frame scores are 5.0. "
            "Pass --min-score below 5.0 or run without --no-vision for "
            "meaningful highlight selection."
        )

    build_edit(
        results,
        output_dir=output_dir,
        save_segments=args.segments,
        min_score=args.min_score,
        half_width=args.highlight_window,
        max_per_clip=args.max_per_clip,
        transition=args.transition,
        transition_duration=args.transition_duration,
        max_reel_duration=args.max_reel_duration,
    )

    if args.music:
        reel_path = output_dir / REEL_FILENAME
        if not reel_path.exists():
            logging.warning("No highlight reel found — skipping music generation")
        else:
            reel_info = probe_clip(reel_path)
            if reel_info is None:
                logging.warning("Could not probe reel duration — skipping music generation")
            else:
                music_prompt = (
                    args.music_prompt
                    or stored_music_prompt
                    or _derive_music_prompt(results)
                )
                logging.info(f"Music prompt: {music_prompt!r}")
                soundtrack_path = output_dir / MUSIC_FILENAME
                _generate_soundtrack(
                    reel_info.duration, music_prompt, args.music_model, soundtrack_path
                )
                _mix_soundtrack(
                    reel_path, soundtrack_path,
                    output_dir / MUSIC_REEL_FILENAME,
                    args.music_volume, DEFAULT_ORIG_AUDIO_VOLUME,
                )


if __name__ == "__main__":
    main()
