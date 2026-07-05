#!/usr/bin/env python3
"""
Free AI Long-Form Video Generator
----------------------------------
Turns a plain-text script into a narrated video: AI-generated images (Pollinations.ai),
AI voiceover with word-accurate captions (edge-tts), Ken Burns motion, and optional
background music -- all stitched together with ffmpeg.

No paid APIs. No API keys required.

Usage:
    python generate_video.py --script scripts/example_script.txt --out output/final_video.mp4
"""

import argparse
import asyncio
import os
import random
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

import requests
import edge_tts

DEFAULT_VOICE = "en-US-AriaNeural"
DEFAULT_WIDTH = 1920
DEFAULT_HEIGHT = 1080
DEFAULT_FPS = 25
WORDS_PER_CAPTION = 6
POLLINATIONS_BASE = "https://image.pollinations.ai/prompt"


# --------------------------------------------------------------------------
# 1. Script parsing
# --------------------------------------------------------------------------

def parse_script(path: str):
    """
    Script format:

        VOICE: en-US-AriaNeural
        MUSIC: assets/lofi.mp3

        ---
        IMAGE: a cozy coffee shop window on a rainy day, warm light, aesthetic
        Rain taps gently against the window as steam rises from a fresh cup of coffee.
        ---
        IMAGE: person journaling by candlelight, cozy bedroom
        There's something quietly powerful about writing your thoughts down before bed.

    Header lines (VOICE / MUSIC) before the first '---' are optional global settings.
    Each scene block optionally starts with "IMAGE: <prompt>". If omitted, the
    narration text itself is used as the image prompt.
    """
    raw = Path(path).read_text(encoding="utf-8")
    blocks = raw.split("---")

    settings = {"voice": DEFAULT_VOICE, "music": None}
    header = blocks[0].strip()
    for line in header.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.upper().startswith("VOICE:"):
            settings["voice"] = line.split(":", 1)[1].strip()
        elif line.upper().startswith("MUSIC:"):
            val = line.split(":", 1)[1].strip()
            settings["music"] = val or None

    scenes = []
    for block in blocks[1:]:
        block = block.strip()
        if not block:
            continue
        lines = [l for l in block.splitlines() if l.strip()]
        image_prompt = None
        narration_lines = []
        for line in lines:
            if line.upper().startswith("IMAGE:") and image_prompt is None:
                image_prompt = line.split(":", 1)[1].strip()
            else:
                narration_lines.append(line.strip())
        narration = " ".join(narration_lines).strip()
        if not narration:
            continue
        if not image_prompt:
            image_prompt = narration[:200]
        scenes.append({"image_prompt": image_prompt, "narration": narration})

    if not scenes:
        raise ValueError("No scenes found. Separate scenes with '---' lines.")

    return settings, scenes


# --------------------------------------------------------------------------
# 2. Narration + word-accurate captions (edge-tts)
# --------------------------------------------------------------------------

async def _synthesize(text: str, voice: str, out_mp3: Path):
    communicate = edge_tts.Communicate(text, voice)
    boundaries = []
    with open(out_mp3, "wb") as f:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                boundaries.append(chunk)
    return boundaries


def synthesize_narration(text: str, voice: str, out_mp3: Path):
    """Runs edge-tts and returns word boundary timing (list of dicts)."""
    return asyncio.run(_synthesize(text, voice, out_mp3))


def _fmt_srt_time(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def build_srt(boundaries, out_srt: Path, words_per_caption=WORDS_PER_CAPTION):
    """Groups word boundaries into short caption chunks with real timing."""
    if not boundaries:
        out_srt.write_text("", encoding="utf-8")
        return

    entries = []
    chunk = []
    for wb in boundaries:
        chunk.append(wb)
        ends_clause = wb["text"].strip().endswith((".", "!", "?", ",", "\u060c", "\u061f"))
        if len(chunk) >= words_per_caption or ends_clause:
            entries.append(chunk)
            chunk = []
    if chunk:
        entries.append(chunk)

    lines = []
    for i, group in enumerate(entries, start=1):
        start = group[0]["offset"] / 1e7
        end = (group[-1]["offset"] + group[-1]["duration"]) / 1e7
        text = " ".join(w["text"] for w in group)
        lines.append(str(i))
        lines.append(f"{_fmt_srt_time(start)} --> {_fmt_srt_time(end)}")
        lines.append(text)
        lines.append("")
    out_srt.write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------
# 3. Image generation (Pollinations.ai -- free, no key)
# --------------------------------------------------------------------------

def fetch_image(prompt: str, out_path: Path, width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT, retries=4):
    seed = random.randint(1, 999_999)
    url = f"{POLLINATIONS_BASE}/{quote(prompt)}?width={width}&height={height}&seed={seed}&nologo=true"
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, timeout=120)
            resp.raise_for_status()
            out_path.write_bytes(resp.content)
            if out_path.stat().st_size > 5000:  # sanity check, not an error page
                return
            last_err = "image response too small"
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
        print(f"  [image] attempt {attempt} failed ({last_err}), retrying...")
        time.sleep(3 * attempt)
    raise RuntimeError(f"Failed to fetch image for prompt {prompt!r}: {last_err}")


# --------------------------------------------------------------------------
# 4. ffmpeg helpers
# --------------------------------------------------------------------------

def run(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n{result.stderr[-3000:]}")
    return result


def get_duration(path: Path) -> float:
    result = run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ])
    return float(result.stdout.strip())


KEN_BURNS_PRESETS = [
    "z='min(zoom+0.0015,1.3)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'",
    "z='if(lte(zoom,1.0),1.3,max(1.001,zoom-0.0015))':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'",
    "z='min(zoom+0.0012,1.25)':x='iw/2-(iw/zoom/2)+50*sin(on/200)':y='ih/2-(ih/zoom/2)'",
]


def build_scene_clip(image_path: Path, audio_path: Path, srt_path: Path,
                      duration: float, out_path: Path, scene_idx: int,
                      width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT, fps=DEFAULT_FPS):
    frames = max(int(duration * fps), fps)
    kb = KEN_BURNS_PRESETS[scene_idx % len(KEN_BURNS_PRESETS)]

    vf_parts = [
        "scale=8000:-1",
        f"zoompan={kb}:d={frames}:s={width}x{height}:fps={fps}",
    ]
    if srt_path.exists() and srt_path.stat().st_size > 0:
        escaped = str(srt_path).replace("\\", "/").replace(":", "\\:")
        vf_parts.append(
            f"subtitles={escaped}:force_style='FontName=DejaVu Sans,FontSize=30,"
            f"PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,"
            f"Outline=2,Shadow=0,Alignment=2,MarginV=70'"
        )
    vf = ",".join(vf_parts)

    run([
        "ffmpeg", "-y",
        "-loop", "1", "-i", str(image_path),
        "-i", str(audio_path),
        "-vf", vf,
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-t", f"{duration:.3f}",
        "-c:a", "aac", "-shortest",
        str(out_path),
        "-loglevel", "error",
    ])


def concat_clips(clip_paths, out_path: Path, workdir: Path):
    list_file = workdir / "concat_list.txt"
    list_file.write_text(
        "\n".join(f"file '{p.resolve()}'" for p in clip_paths), encoding="utf-8"
    )
    run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(list_file), "-c", "copy", str(out_path), "-loglevel", "error",
    ])


def add_background_music(video_path: Path, music_path: Path, out_path: Path, volume=0.12):
    run([
        "ffmpeg", "-y",
        "-i", str(video_path), "-stream_loop", "-1", "-i", str(music_path),
        "-filter_complex",
        f"[1:a]volume={volume}[music];[0:a][music]amix=inputs=2:duration=first:dropout_transition=2[aout]",
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "copy", "-shortest", str(out_path),
        "-loglevel", "error",
    ])


# --------------------------------------------------------------------------
# 5. Orchestration
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Generate a long-form AI video from a script.")
    ap.add_argument("--script", required=True, help="Path to the script .txt file")
    ap.add_argument("--out", default="output/final_video.mp4", help="Output video path")
    ap.add_argument("--voice", default=None, help="Override the TTS voice for all scenes")
    ap.add_argument("--music", default=None, help="Override/add a background music file")
    ap.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    ap.add_argument("--height", type=int, default=DEFAULT_HEIGHT)
    ap.add_argument("--keep-temp", action="store_true", help="Keep intermediate files")
    args = ap.parse_args()

    settings, scenes = parse_script(args.script)
    voice = args.voice or settings["voice"]
    music = args.music or settings["music"]

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    workdir = out_path.parent / "_work"
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)

    print(f"Loaded {len(scenes)} scene(s). Voice: {voice}. Music: {music or 'none'}")

    clip_paths = []
    for i, scene in enumerate(scenes):
        print(f"\n[{i+1}/{len(scenes)}] Narration: {scene['narration'][:70]}...")
        audio_path = workdir / f"scene_{i:03}.mp3"
        srt_path = workdir / f"scene_{i:03}.srt"
        image_path = workdir / f"scene_{i:03}.jpg"
        clip_path = workdir / f"scene_{i:03}.mp4"

        print("  -> generating narration + captions (edge-tts)")
        boundaries = synthesize_narration(scene["narration"], voice, audio_path)
        build_srt(boundaries, srt_path)
        duration = get_duration(audio_path) + 0.4  # small tail padding

        print(f"  -> generating image: {scene['image_prompt'][:70]}...")
        fetch_image(scene["image_prompt"], image_path, args.width, args.height)

        print(f"  -> rendering scene clip ({duration:.1f}s)")
        build_scene_clip(image_path, audio_path, srt_path, duration, clip_path, i,
                          args.width, args.height)
        clip_paths.append(clip_path)

    print("\nConcatenating all scenes...")
    no_music_path = workdir / "combined_no_music.mp4"
    concat_clips(clip_paths, no_music_path, workdir)

    if music and Path(music).exists():
        print("Mixing background music...")
        add_background_music(no_music_path, Path(music), out_path)
    else:
        if music:
            print(f"  (music file '{music}' not found, skipping)")
        shutil.copy(no_music_path, out_path)

    if not args.keep_temp:
        shutil.rmtree(workdir, ignore_errors=True)

    total_duration = get_duration(out_path)
    print(f"\nDone! Video saved to: {out_path} ({total_duration:.1f}s)")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"\nERROR: {e}", file=sys.stderr)
        sys.exit(1)
