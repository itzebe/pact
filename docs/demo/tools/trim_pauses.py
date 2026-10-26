"""Compress redundant pauses in the enhanced demo without dropping any footage.

The demo's narration has ~1s of silence at every sentence end. The hackathon
caps the video at three minutes, so those pauses are shortened to `KEEP` seconds
while every spoken word, every scene and every evidence card is retained.

Audio and video are cut at the same time intervals in one pass, so they stay in
sync: both use the same `between(t, start, end)` keep-list, then re-timestamp.

Usage:
  python trim_pauses.py <in.mp4> <out.mp4> [keep_seconds] [noise_db] [min_gap]
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

FF = subprocess.run([sys.executable, "-c",
                     "import imageio_ffmpeg;print(imageio_ffmpeg.get_ffmpeg_exe())"],
                    capture_output=True, text=True).stdout.strip()

V = ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
     "-r", "30", "-video_track_timescale", "15360"]
A = ["-c:a", "aac", "-b:a", "128k", "-ar", "24000", "-ac", "1"]


def duration(path: str) -> float:
    err = subprocess.run([FF, "-hide_banner", "-i", path],
                         capture_output=True, text=True).stderr
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def silences(path: str, noise: float, min_gap: float) -> list[tuple[float, float]]:
    err = subprocess.run([FF, "-hide_banner", "-i", path, "-af",
                          f"silencedetect=noise={noise}dB:d={min_gap}",
                          "-f", "null", "-"], capture_output=True, text=True).stderr
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", err)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", err)]
    return [(s, e) for s, e in zip(starts, ends)]


def keep_intervals(gaps, keep: float, total: float) -> list[tuple[float, float]]:
    """Everything except the middle of each pause; `keep` seconds survive."""
    out: list[tuple[float, float]] = []
    cur = 0.0
    for s, e in gaps:
        if e - s <= keep:
            continue
        if s + keep > cur:
            out.append((cur, s + keep))
        cur = e
    if cur < total:
        out.append((cur, total))
    return out


def main() -> None:
    src, dst = sys.argv[1], sys.argv[2]
    keep = float(sys.argv[3]) if len(sys.argv) > 3 else 0.65
    noise = float(sys.argv[4]) if len(sys.argv) > 4 else -35.0
    min_gap = float(sys.argv[5]) if len(sys.argv) > 5 else 0.45

    total = duration(src)
    gaps = silences(src, noise, min_gap)
    keep_iv = keep_intervals(gaps, keep, total)
    saved = total - sum(e - s for s, e in keep_iv)
    print(f"  source {total:.2f}s, {len(gaps)} pauses, trimming {saved:.2f}s "
          f"-> projected {total - saved:.2f}s")

    expr = "+".join(f"between(t\\,{s:.3f}\\,{e:.3f})" for s, e in keep_iv)
    vf = f"select='{expr}',setpts=N/FRAME_RATE/TB"
    af = f"aselect='{expr}',asetpts=N/SR/TB"

    subprocess.run([FF, "-y", "-i", src, "-filter_complex",
                    f"[0:v]{vf}[v];[0:a]{af}[a]", "-map", "[v]", "-map", "[a]",
                    *V, *A, "-movflags", "+faststart", dst], check=True)
    print(f"  wrote {dst} ({duration(dst):.2f}s)")


if __name__ == "__main__":
    main()
