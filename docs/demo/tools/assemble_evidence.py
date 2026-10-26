"""Splice the evidence cards into the PACT demo without altering it.

Layout:
  original[0:81]  + [card_p, card_s] + original[81:135] + [card_n, card_o] + original[135:]

81.0 is the cut where the original leaves the PayPal Sandbox checkout for the
verification panel, so the payment evidence lands right after the payment
section. 135.0 is where the ledger narration ends, so PACT's own captures land
before the closing shot. The original mp4 is only ever read.

Usage:
  python assemble_evidence.py <cards_dir> <audio_dir> <work_dir>
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ORIG = ROOT / "docs" / "demo" / "pact-demo.mp4"
FINAL = ROOT / "docs" / "demo" / "pact-demo-enhanced.mp4"

SPLICE_A, SPLICE_B = 81.0, 135.0
TAIL = 0.45
FPS = 30
V = ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
     "-r", str(FPS), "-video_track_timescale", "15360"]
A = ["-c:a", "aac", "-b:a", "128k", "-ar", "24000", "-ac", "1"]

SEG_A = [("card_p", "a1"), ("card_s", "a2")]
SEG_B = [("card_n", "b1"), ("card_o", "b2")]

FF = subprocess.run([sys.executable, "-c",
                     "import imageio_ffmpeg;print(imageio_ffmpeg.get_ffmpeg_exe())"],
                    capture_output=True, text=True).stdout.strip()


def run(args):
    p = subprocess.run([str(a) for a in args], capture_output=True, text=True)
    if p.returncode:
        print("FAILED:", " ".join(str(a) for a in args))
        print(p.stderr[-2500:])
        sys.exit(1)
    return p


def dur(path: Path) -> float:
    err = subprocess.run([FF, "-hide_banner", "-i", str(path)],
                         capture_output=True, text=True).stderr
    line = [l for l in err.splitlines() if "Duration" in l][0]
    h, m, s = line.split("Duration:")[1].split(",")[0].strip().split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def card_part(work: Path, cards: Path, audio: Path, name: str, card: str,
              beat: str, fade_in: bool, fade_out: bool) -> Path:
    out = work / f"{name}.mp4"
    d = dur(audio / f"{beat}.wav") + TAIL
    vf = ("zoompan=z='min(1+0.030*on/900,1.030)':x='iw/2-(iw/zoom/2)':"
          "y='ih/2-(ih/zoom/2)':d=1:s=1600x900:fps=30,format=yuv420p")
    af = []
    if fade_in:
        vf += ",fade=t=in:st=0:d=0.4"
        af.append("afade=t=in:st=0:d=0.4")
    if fade_out:
        vf += f",fade=t=out:st={d - 0.4:.2f}:d=0.4"
        af.append(f"afade=t=out:st={d - 0.5:.2f}:d=0.5")
    chain = ",".join(af) if af else "anull"
    run([FF, "-y", "-loop", "1", "-i", cards / f"{card}.png", "-i", audio / f"{beat}.wav",
         "-filter_complex", f"[0:v]{vf}[v];[1:a]aresample=24000,{chain},apad[a]",
         "-map", "[v]", "-map", "[a]", "-t", f"{d:.3f}", "-shortest", *V, *A, out])
    return out


def segment(work: Path, cards: Path, audio: Path, tag: str, specs) -> Path:
    parts = []
    for i, (card, beat) in enumerate(specs):
        p = card_part(work, cards, audio, f"{tag}_{card}", card, beat,
                      i == 0, i == len(specs) - 1)
        parts.append(p)
        print(f"  {tag} {card}: {dur(p):.2f}s")
    lst = work / f"{tag}.txt"
    lst.write_text("".join(f"file '{p}'\n" for p in parts))
    out = work / f"{tag}.mp4"
    run([FF, "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", out])
    print(f"  {tag} total: {dur(out):.2f}s")
    return out


def slice_orig(work: Path, start: float, end: float | None, name: str) -> Path:
    out = work / f"{name}.mp4"
    if start:
        args = [FF, "-y", "-ss", f"{start:.3f}", "-i", ORIG]
    else:
        args = [FF, "-y", "-i", ORIG]
    if end is not None:
        args += ["-t", f"{end - start:.3f}"]
    run(args + [*V, *A, out])
    return out


def main() -> None:
    # Resolve to absolute paths: ffmpeg resolves concat-list entries relative
    # to the list file, not the working directory.
    cards = Path(sys.argv[1]).resolve()
    audio = Path(sys.argv[2]).resolve()
    work = Path(sys.argv[3]).resolve()
    work.mkdir(parents=True, exist_ok=True)

    print("building evidence segments")
    seg_a = segment(work, cards, audio, "segA", SEG_A)
    seg_b = segment(work, cards, audio, "segB", SEG_B)

    print("slicing original")
    o1 = slice_orig(work, 0.0, SPLICE_A, "orig1")
    o2 = slice_orig(work, SPLICE_A, SPLICE_B, "orig2")
    o3 = slice_orig(work, SPLICE_B, None, "orig3")
    for n, p in [("orig1", o1), ("orig2", o2), ("orig3", o3)]:
        print(f"  {n}: {dur(p):.2f}s")

    lst = work / "final.txt"
    lst.write_text("".join(f"file '{p}'\n" for p in [o1, seg_a, o2, seg_b, o3]))
    run([FF, "-y", "-f", "concat", "-safe", "0", "-i", lst,
         "-c", "copy", "-movflags", "+faststart", FINAL])
    print(f"wrote {FINAL} ({dur(FINAL):.2f}s)")


if __name__ == "__main__":
    main()
