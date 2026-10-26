"""Splice the evidence cards into the original PACT demo.

Layout:  original[0:SPLICE]  +  evidence(card_a, card_b, card_c)  +  original[SPLICE:]

SPLICE is the measured boundary where the original video cuts from the ledger
panel to the closing shot, so the insert lands at the end of the settlement
narrative and before the close. The original mp4 is only ever read.

Run:  python assemble_enhanced.py            # needs edge-tts, imageio-ffmpeg, Pillow
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import edge_tts

ROOT = Path(__file__).resolve().parents[3]
DEMO = ROOT / "docs" / "demo"
ORIG = DEMO / "pact-demo.mp4"
FINAL = DEMO / "pact-demo-enhanced.mp4"
WORK = Path("/tmp/pact-evidence-build")

SPLICE = 133.0        # measured ledger -> closing boundary in the original
TAIL = 0.40           # hold each card a beat after its narration ends
FPS = 30
VOICE = "en-US-GuyNeural"

V = ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
     "-r", str(FPS), "-video_track_timescale", "15360"]
A = ["-c:a", "aac", "-b:a", "128k", "-ar", "24000", "-ac", "1"]

FF = subprocess.run([sys.executable, "-c",
                     "import imageio_ffmpeg;print(imageio_ffmpeg.get_ffmpeg_exe())"],
                    capture_output=True, text=True).stdout.strip()


def run(args):
    p = subprocess.run([str(a) for a in args], capture_output=True, text=True)
    if p.returncode:
        print("FAILED:", " ".join(str(a) for a in args))
        print(p.stderr[-3000:])
        sys.exit(1)
    return p


def dur(path: Path) -> float:
    err = subprocess.run([FF, "-hide_banner", "-i", str(path)],
                         capture_output=True, text=True).stderr
    line = [l for l in err.splitlines() if "Duration" in l][0]
    h, m, s = line.split("Duration:")[1].split(",")[0].strip().split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def synthesise(narration: dict[str, str], outdir: Path) -> dict[str, Path]:
    async def go():
        for k, text in narration.items():
            await edge_tts.Communicate(text, VOICE).save(outdir / f"{k}.mp3")
    asyncio.run(go())
    wavs = {}
    for k in narration:
        w = outdir / f"{k}.wav"
        run([FF, "-y", "-i", outdir / f"{k}.mp3", "-ar", "24000", "-ac", "1", w])
        wavs[k] = w
    return wavs


def evidence_part(name: str, png: Path, narration: Path, d: float,
                  fade_in: bool, fade_out: bool) -> Path:
    out = WORK / f"{name}.mp4"
    vf = ("zoompan=z='min(1+0.035*on/900,1.035)':x='iw/2-(iw/zoom/2)':"
          "y='ih/2-(ih/zoom/2)':d=1:s=1600x900:fps=30,format=yuv420p")
    af = []
    if fade_in:
        vf += ",fade=t=in:st=0:d=0.35"
        af.append("afade=t=in:st=0:d=0.35")
    if fade_out:
        vf += f",fade=t=out:st={d - 0.35:.2f}:d=0.35"
        af.append(f"afade=t=out:st={d - 0.5:.2f}:d=0.5")
    chain = ",".join(af) if af else "anull"
    run([FF, "-y", "-loop", "1", "-i", png, "-i", narration,
         "-filter_complex", f"[0:v]{vf}[v];[1:a]aresample=24000,{chain},apad[a]",
         "-map", "[v]", "-map", "[a]", "-t", f"{d:.3f}", "-shortest", *V, *A, out])
    return out


def main() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    cards = DEMO / "evidence-cards"
    if not cards.exists():
        print("cards missing; run: python docs/demo/tools/evidence_cards.py "
              f"{cards}")
        sys.exit(1)

    narration = __import__("json").loads(
        (Path(__file__).parent / "evidence_narration.json").read_text())
    narration = {k: v for k, v in narration.items() if not k.startswith("_")}
    wavs = synthesise(narration, WORK)

    beats = [("card_a", "e1"), ("card_b", "e2"), ("card_c", "e3")]
    parts = []
    for i, (card, beat) in enumerate(beats):
        d = dur(wavs[beat]) + TAIL
        parts.append(evidence_part(f"ev_{card}", cards / f"{card}.png", wavs[beat],
                                   d, i == 0, i == len(beats) - 1))
        print(f"  {card}: {d:.2f}s")

    lst = WORK / "ev.txt"
    lst.write_text("".join(f"file '{p}'\n" for p in parts))
    evidence = WORK / "evidence.mp4"
    run([FF, "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", evidence])

    seg1, seg2 = WORK / "seg1.mp4", WORK / "seg2.mp4"
    run([FF, "-y", "-i", ORIG, "-t", f"{SPLICE:.3f}", *V, *A, seg1])
    run([FF, "-y", "-ss", f"{SPLICE:.3f}", "-i", ORIG, *V, *A, seg2])

    lst.write_text("".join(f"file '{p}'\n" for p in [seg1, evidence, seg2]))
    run([FF, "-y", "-f", "concat", "-safe", "0", "-i", lst,
         "-c", "copy", "-movflags", "+faststart", FINAL])
    print(f"wrote {FINAL} ({dur(FINAL):.2f}s)")


if __name__ == "__main__":
    main()
