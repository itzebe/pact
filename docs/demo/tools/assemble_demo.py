"""Assemble docs/demo/pact-demo.mp4 from the clips record_demo.py captured.

Each scene is normalised to 1600x900, trimmed to the narration beat it
illustrates, given an environment lower-third, and muxed with the narration.
Scenes are then concatenated into the final H.264 mp4.

    python assemble_demo.py
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUILD = HERE / "build"
OUT = BUILD / "out"
OUT.mkdir(parents=True, exist_ok=True)
W, H, FPS = 1600, 900, 30
VOICE_LABEL_APP = "label_app.png"
VOICE_LABEL_PAYPAL = "label_paypal.png"


def ff() -> str:
    return subprocess.run(
        [sys.executable, "-c", "import imageio_ffmpeg;print(imageio_ffmpeg.get_ffmpeg_exe())"],
        capture_output=True, text=True, check=True).stdout.strip()


FF = ff()
CLIPS = json.loads((BUILD / "scene_clips.json").read_text())


def dur(path: Path) -> float:
    o = subprocess.run([FF, "-hide_banner", "-i", str(path)], capture_output=True, text=True).stderr
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", o)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def title_card() -> None:
    """Regenerate the title card and lower-thirds via gen_assets.py."""
    subprocess.run([sys.executable, str(HERE / "gen_assets.py")], check=True)


def piece(src: Path, kind: str, start: float, length: float, label: str | None, i: int) -> Path:
    p = OUT / f"p{i}.mp4"
    if kind == "img":
        subprocess.run([FF, "-y", "-loop", "1", "-i", str(src), "-t", f"{length}",
                        "-vf", f"zoompan=z='min(zoom+0.0004,1.10)':d={int(length*FPS)}:s={W}x{H}:fps={FPS},format=yuv420p",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(p)], check=True, capture_output=True)
    else:
        vf = (f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
              f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=0x08090c,setsar=1,fps={FPS}")
        subprocess.run([FF, "-y", "-ss", f"{start}", "-t", f"{length}", "-i", str(src),
                        "-vf", vf, "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(p)],
                       check=True, capture_output=True)
    if label:
        lp = OUT / f"p{i}_l.mp4"
        subprocess.run([FF, "-y", "-i", str(p), "-i", str(BUILD / label),
                        "-filter_complex", "[0:v][1:v]overlay=0:0:format=auto",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(lp)], check=True, capture_output=True)
        return lp
    return p


def mux(video: Path, audio: Path, out: Path, length: float) -> None:
    subprocess.run([FF, "-y", "-i", str(video), "-i", str(audio), "-t", f"{length}",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-af", "apad",
                    str(out)], check=True, capture_output=True)


def main() -> None:
    title_card()
    d = {k: dur(BUILD / f"{k}.mp3") for k in
         ["s0_title", "s1_problem", "s2_agreement", "s3_payment", "s4_safety", "s5_ledger", "s6_close"]}
    scenes = [
        ("s0_title", [("title.png", "img", 0.0, d["s0_title"], None)]),
        ("s1_problem", [("s1", "vid", 3.0, d["s1_problem"], VOICE_LABEL_APP)]),
        ("s2_agreement", [("s2", "vid", 3.5, d["s2_agreement"], VOICE_LABEL_APP)]),
        ("s3_payment", [("s3a", "vid", 3.5, max(6.0, d["s3_payment"] - 10.0), VOICE_LABEL_APP),
                        ("s3b", "vid", 1.5, 10.0, VOICE_LABEL_PAYPAL)]),
        ("s4_safety", [("s4", "vid", 4.5, d["s4_safety"], VOICE_LABEL_APP)]),
        ("s5_ledger", [("s5", "vid", 4.5, d["s5_ledger"], VOICE_LABEL_APP)]),
        ("s6_close", [("s6", "vid", 2.0, d["s6_close"], VOICE_LABEL_APP)]),
    ]
    finals = []
    i = 0
    for key, parts in scenes:
        built = []
        for ck, kind, start, length, label in parts:
            src = BUILD / ck if kind == "img" else Path(CLIPS[ck])
            built.append(piece(src, kind, start, length, label, i))
            i += 1
        if len(built) == 1:
            joined = built[0]
        else:
            lst = OUT / f"cat_{key}.txt"
            lst.write_text("".join(f"file '{p}'\n" for p in built))
            joined = OUT / f"joined_{key}.mp4"
            subprocess.run([FF, "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                            "-c", "copy", str(joined)], check=True, capture_output=True)
        fin = OUT / f"final_{key}.mp4"
        mux(joined, BUILD / f"{key}.mp3", fin, d[key])
        finals.append(fin)

    lst = OUT / "final.txt"
    lst.write_text("".join(f"file '{p}'\n" for p in finals))
    video = BUILD / "pact-demo.mp4"
    subprocess.run([FF, "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(FPS),
                    "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
                    str(video)], check=True, capture_output=True)
    print("FINAL:", video, f"{dur(video):.1f}s")


if __name__ == "__main__":
    main()
