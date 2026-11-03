"""Assemble the final narrated demo from the recorded walkthrough.

Retimes each beat of /tmp/final_rec/*.webm to its narration, muxes the genuine
edge-tts narration, burns captions and a persistent Sandbox label, and
concatenates the beats into one H.264 MP4.

    python docs/judge-demo/tools/assemble_final.py
"""
from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import imageio_ffmpeg

FF = imageio_ffmpeg.get_ffmpeg_exe()
REC = Path("/tmp/final_rec")
WORK = REC / "work"
WORK.mkdir(parents=True, exist_ok=True)
SRC = sorted(glob.glob(str(REC / "*.webm")), key=os.path.getsize, reverse=True)[0]
NAR = json.loads(Path("docs/judge-demo/tools/narration.json").read_text())
META = json.loads((REC / "beats.json").read_text())
BEATS = META["beats"]
OUT = Path("docs/judge-demo/pact-demo-final-narrated.mp4")
W, H = 1600, 900
GAP = 1.5
INTRO_HOLD = 3.0
OUTRO_HOLD = 6.0
FONT = "DejaVu Sans"

CAPTIONS = {
 "b1": ["Two strangers agree to do a job.", "One fears paying and getting nothing.",
        "The other fears delivering and never being paid.", "Neither wants another account."],
 "b2": ["This is Pact — it opens into a guest workspace.",
        "No sign-up, no form, no email.",
        "A live model, a durable database, the real PayPal Sandbox.",
        "And the workspace is mine alone."],
 "b3": ["I post a job: brief, acceptance criteria, a $200 ceiling.",
        "Seller agents quote; the buyer counters; the price converges.",
        "Every round is recorded — the budget is enforced in code.",
        "Sellers are ranked by verified-delivery reputation."],
 "b4": ["Funds are held before work starts.",
        "PayPal requires the buyer to approve — so Pact stops and hands over the link.",
        "I approve in the real PayPal Sandbox. No real money moves.",
        "Back in Pact, it authorizes the hold."],
 "b5": ["An independent verifier checks every acceptance criterion.",
        "Only a passing verdict releases the money.",
        "PayPal confirms the capture.",
        "Every step is in a hash-chained ledger you can re-verify."],
 "b6": ["Need to reverse it? The refund runs through PayPal too.",
        "Capped at the captured amount and deduplicated.",
        "A failed verification would have voided the hold instead.",
        "The buyer would be charged nothing."],
 "b7": ["Pact doesn't ask strangers to trust each other blindly.",
        "It gives their agreement a payment lifecycle they can verify.",
        "A real hold, a real capture, a real refund — and a record of all three."],
}


def dur(p) -> float:
    o = subprocess.run([FF, "-hide_banner", "-i", str(p)], capture_output=True, text=True).stderr
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", o)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def ass_ts(t: float) -> str:
    h = int(t // 3600); m = int((t % 3600) // 60); s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def caption_ass(start: float, span: float, lines: list[str]) -> str:
    """Concise captions, one line-group per quarter of the beat."""
    per = span / len(lines)
    head = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1600\nPlayResY: 900\n"
        "WrapStyle: 0\n\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, "
        "Bold, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Cap,{FONT},38,&H00FFFFFF,&H00000000,&H80000000,-1,1,3,1,2,120,120,54,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    ev = ""
    for i, line in enumerate(lines):
        s = start + i * per
        e = start + (i + 1) * per - 0.05
        ev += f"Dialogue: 0,{ass_ts(s)},{ass_ts(e)},Cap,,0,0,0,,{line}\n"
    return head + ev


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        sys.stderr.write(r.stderr[-1500:]); raise SystemExit(f"ffmpeg failed: {' '.join(cmd[:6])}...")


def main():
    total = dur(SRC)
    order = ["b1", "b2", "b3", "b4", "b5", "b6", "b7"]
    bounds = {k: BEATS[k] for k in order}
    bounds["end"] = total
    # narration durations come from the mp3s
    nardur = {}
    for k in order:
        mp3 = f"/tmp/nar/{k}.mp3"
        nardur[k] = dur(mp3)

    segments, ass_all, cursor = [], "", 0.0
    # gap (black) clip
    gap = WORK / "gap.mp4"
    if not gap.exists():
        run([FF, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
             f"color=c=0x0b0f14:s={W}x{H}:r=30", "-f", "lavfi", "-i",
             "anullsrc=channel_layout=stereo:sample_rate=44100", "-t", str(GAP),
             "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac",
             "-b:a", "128k", "-shortest", str(gap)])

    for i, k in enumerate(order):
        s = bounds[k]
        e = bounds[order[i + 1]] if i + 1 < len(order) else total
        src_len = max(e - s, 0.1)
        tgt = nardur[k]
        ratio = tgt / src_len
        seg = WORK / f"{k}.mp4"
        run([FF, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{s:.2f}", "-to", f"{e:.2f}",
             "-i", SRC, "-i", f"/tmp/nar/{k}.mp3",
             "-filter_complex", f"[0:v]setpts={ratio:.5f}*PTS,scale={W}:{H},fps=30[v]",
             "-map", "[v]", "-map", "1:a", "-c:v", "libx264", "-crf", "20",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
             "-t", f"{tgt:.2f}", str(seg)])
        # extend the opening title card and the closing card with a silent hold
        if k == "b1":
            hold = WORK / "hold1.mp4"
            run([FF, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                 f"color=c=0x0b0f14:s={W}x{H}:r=30", "-f", "lavfi", "-i",
                 "anullsrc=channel_layout=stereo:sample_rate=44100", "-t", str(INTRO_HOLD),
                 "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac",
                 "-b:a", "128k", "-shortest", str(hold)])
            segments += [str(hold), str(seg)]
            cursor += INTRO_HOLD
        else:
            segments.append(str(seg))
        if k == "b7":
            hold = WORK / "hold7.mp4"
            run([FF, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                 f"color=c=0x0b0f14:s={W}x{H}:r=30", "-f", "lavfi", "-i",
                 "anullsrc=channel_layout=stereo:sample_rate=44100", "-t", str(OUTRO_HOLD),
                 "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac",
                 "-b:a", "128k", "-shortest", str(hold)])
            segments.append(str(hold))
        else:
            segments.append(str(gap))
        # captions timed from the segment start
        ass_all += caption_ass(cursor, tgt, CAPTIONS[k])
        cursor += tgt + (GAP if k != "b7" else 0)

    # concat list
    lst = WORK / "list.txt"
    lst.write_text("".join(f"file '{p}'\n" for p in segments))
    joined = WORK / "joined.mp4"
    run([FF, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", str(lst), "-c", "copy", str(joined)])

    # burn captions + persistent Sandbox label
    ass = WORK / "caps.ass"
    ass.write_text(ass_all)
    vf = (f"subtitles={ass}:fontsdir=/usr/share/fonts/truetype/dejavu,"
          f"drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
          f"text='PAYPAL SANDBOX · TEST ENVIRONMENT':fontsize=26:fontcolor=white:"
          f"box=1:boxcolor=0x0b0f14@0.72:boxborderw=12:x=w-tw-24:y=24")
    run([FF, "-y", "-hide_banner", "-loglevel", "error", "-i", str(joined), "-vf", vf,
         "-c:v", "libx264", "-crf", "20", "-preset", "medium", "-pix_fmt", "yuv420p",
         "-c:a", "copy", "-movflags", "+faststart", str(OUT)])

    print("OUT:", OUT, "|", round(dur(OUT), 1), "s |", OUT.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
