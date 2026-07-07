#!/usr/bin/env python3
"""
Free AI Animated Video Generator
----------------------------------
Turns a plain-text script into a video of YOUR character actually talking:
real word-accurate lip-sync (Rhubarb Lip Sync), blinking, a subtle idle bob,
and AI-generated scene backgrounds behind it (Pollinations.ai) -- all free,
no API keys, fully automatable.

Usage:
    python generate_video.py --script scripts/example_script.txt --out output/final_video.mp4
"""

import argparse
import asyncio
import json
import math
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

import requests
import edge_tts
from PIL import Image

DEFAULT_VOICE = "en-US-AriaNeural"
CANVAS_W, CANVAS_H = 1920, 1080
FPS = 25
POLLINATIONS_BASE = "https://image.pollinations.ai/prompt"
CHARACTER_DIR = Path(__file__).parent / "assets" / "character"

VISEME_MAP = {
    "A": "closed", "B": "closed", "G": "closed", "X": "closed",
    "C": "mid", "E": "mid", "F": "mid", "H": "mid",
    "D": "wide",
}


def parse_script(path: str):
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


async def _synthesize(text: str, voice: str, out_mp3: Path):
    communicate = edge_tts.Communicate(text, voice)
    with open(out_mp3, "wb") as f:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])


def synthesize_narration(text: str, voice: str, out_mp3: Path):
    asyncio.run(_synthesize(text, voice, out_mp3))


def fetch_image(prompt: str, out_path: Path, width=CANVAS_W, height=CANVAS_H, retries=4):
    seed = random.randint(1, 999_999)
    url = f"{POLLINATIONS_BASE}/{quote(prompt)}?width={width}&height={height}&seed={seed}&nologo=true"
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, timeout=120)
            resp.raise_for_status()
            out_path.write_bytes(resp.content)
            if out_path.stat().st_size > 5000:
                return
            last_err = "image response too small"
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
        print(f"  [image] attempt {attempt} failed ({last_err}), retrying...")
        time.sleep(3 * attempt)
    raise RuntimeError(f"Failed to fetch image for prompt {prompt!r}: {last_err}")


def run(cmd, **kwargs):
    result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    if result.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(str(c) for c in cmd)}\n{result.stderr[-3000:]}")
    return result


def get_duration(path: Path) -> float:
    result = run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ])
    return float(result.stdout.strip())


def mp3_to_wav(mp3_path: Path, wav_path: Path):
    run(["ffmpeg", "-y", "-i", str(mp3_path), "-ar", "22050", "-ac", "1", str(wav_path), "-loglevel", "error"])


def run_rhubarb(wav_path: Path, out_json: Path, rhubarb_bin="rhubarb"):
    """Runs Rhubarb Lip Sync and returns a list of {start, end, value} mouth cues."""
    try:
        run([
            rhubarb_bin, "-f", "json", "-o", str(out_json),
            "--recognizer", "phonetic",
            str(wav_path),
        ])
        data = json.loads(out_json.read_text(encoding="utf-8"))
        return data.get("mouthCues", [])
    except Exception as e:  # noqa: BLE001
        print(f"  [lipsync] Rhubarb unavailable/failed ({e}); falling back to a simple approximation.")
        return None


def approximate_mouth_cues(wav_path: Path, duration: float):
    """Fallback if Rhubarb isn't installed: derive rough mouth movement from audio volume."""
    import wave
    import audioop

    cues = []
    try:
        with wave.open(str(wav_path), "rb") as wf:
            framerate = wf.getframerate()
            chunk_ms = 90
            chunk_frames = int(framerate * chunk_ms / 1000)
            t = 0.0
            while True:
                frames = wf.readframes(chunk_frames)
                if not frames:
                    break
                rms = audioop.rms(frames, wf.getsampwidth())
                if rms < 200:
                    letter = "X"
                elif rms < 800:
                    letter = "B"
                elif rms < 2000:
                    letter = "C"
                else:
                    letter = "D"
                cues.append({"start": t, "end": t + chunk_ms / 1000, "value": letter})
                t += chunk_ms / 1000
    except Exception as e:  # noqa: BLE001
        print(f"  [lipsync] fallback also failed ({e}); using static mid-mouth for whole clip.")
        cues = [{"start": 0, "end": duration, "value": "C"}]
    return cues


def mouth_state_at(cues, t: float) -> str:
    for cue in cues:
        if cue["start"] <= t < cue["end"]:
            return VISEME_MAP.get(cue["value"], "closed")
    return "closed"


class CharacterRig:
    def __init__(self, character_dir: Path):
        cfg = json.loads((character_dir / "rig.json").read_text())
        self.left_eye_pos = tuple(cfg["left_eye_pos"])
        self.right_eye_pos = tuple(cfg["right_eye_pos"])
        self.mouth_pos = tuple(cfg["mouth_pos"])
        self.canvas_size = tuple(cfg["canvas_size"])

        base = Image.open(character_dir / "base.png").convert("RGBA")
        eye_l_open = Image.open(character_dir / "eye_l_open.png")
        eye_r_open = Image.open(character_dir / "eye_r_open.png")
        eye_closed = Image.open(character_dir / "eye_closed.png")
        mouths = {
            "closed": Image.open(character_dir / "mouth_closed.png"),
            "mid": Image.open(character_dir / "mouth_mid.png"),
            "wide": Image.open(character_dir / "mouth_wide.png"),
        }

        self.frames = {}
        for eyes_state in ("open", "closed"):
            for mouth_state, mouth_img in mouths.items():
                frame = base.copy()
                if eyes_state == "open":
                    frame.paste(eye_l_open, self.left_eye_pos, eye_l_open)
                    frame.paste(eye_r_open, self.right_eye_pos, eye_r_open)
                else:
                    frame.paste(eye_closed, self.left_eye_pos, eye_closed)
                    frame.paste(eye_closed, self.right_eye_pos, eye_closed)
                frame.paste(mouth_img, self.mouth_pos, mouth_img)
                self.frames[(eyes_state, mouth_state)] = frame

    def get(self, eyes_state: str, mouth_state: str) -> Image.Image:
        return self.frames[(eyes_state, mouth_state)]


def make_blink_schedule(duration: float, min_gap=2.5, max_gap=5.5, blink_len=0.14):
    schedule = []
    t = random.uniform(1.0, min_gap)
    while t < duration:
        schedule.append((t, t + blink_len))
        t += random.uniform(min_gap, max_gap)
    return schedule


def is_blinking(schedule, t: float) -> bool:
    for start, end in schedule:
        if start <= t < end:
            return True
    return False


def prepare_background(image_path: Path, width, height) -> Image.Image:
    img = Image.open(image_path).convert("RGB")
    src_ratio = img.width / img.height
    dst_ratio = width / height
    if src_ratio > dst_ratio:
        new_h = height
        new_w = int(height * src_ratio)
    else:
        new_w = width
        new_h = int(width / src_ratio)
    img = img.resize((new_w, new_h))
    left = (new_w - width) // 2
    top = (new_h - height) // 2
    return img.crop((left, top, left + width, top + height))


def render_scene(background: Image.Image, rig: CharacterRig, mouth_cues, blink_schedule,
                  duration: float, audio_path: Path, out_path: Path,
                  char_scale=0.85, fps=FPS):
    char_h = int(CANVAS_H * char_scale)
    char_w = int(char_h * rig.canvas_size[0] / rig.canvas_size[1])
    anchor_x = (CANVAS_W - char_w) // 2
    anchor_y = CANVAS_H - char_h + 20

    resized_cache = {
        key: img.resize((char_w, char_h), Image.LANCZOS)
        for key, img in rig.frames.items()
    }

    total_frames = max(int(duration * fps), fps)

    ffmpeg_cmd = [
        "ffmpeg", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{CANVAS_W}x{CANVAS_H}", "-r", str(fps),
        "-i", "-",
        "-i", str(audio_path),
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest",
        str(out_path), "-loglevel", "error",
    ]
    proc = subprocess.Popen(ffmpeg_cmd, stdin=subprocess.PIPE)

    bg_rgba = background.convert("RGBA")

    for i in range(total_frames):
        t = i / fps
        mouth_state = mouth_state_at(mouth_cues, t) if mouth_cues else "closed"
        eyes_state = "closed" if is_blinking(blink_schedule, t) else "open"
        char_img = resized_cache[(eyes_state, mouth_state)]

        bob = int(5 * math.sin(2 * math.pi * t / 2.6))
        frame = bg_rgba.copy()
        frame.alpha_composite(char_img, (anchor_x, anchor_y + bob))

        proc.stdin.write(frame.convert("RGB").tobytes())

    proc.stdin.close()
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg frame encoding failed for {out_path}")


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


def main():
    ap = argparse.ArgumentParser(description="Generate an animated talking-character video from a script.")
    ap.add_argument("--script", required=True)
    ap.add_argument("--out", default="output/final_video.mp4")
    ap.add_argument("--voice", default=None)
    ap.add_argument("--music", default=None)
    ap.add_argument("--character-scale", type=float, default=0.85)
    ap.add_argument("--rhubarb-bin", default="rhubarb")
    ap.add_argument("--keep-temp", action="store_true")
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
    rig = CharacterRig(CHARACTER_DIR)

    clip_paths = []
    for i, scene in enumerate(scenes):
        print(f"\n[{i+1}/{len(scenes)}] {scene['narration'][:70]}...")
        audio_mp3 = workdir / f"scene_{i:03}.mp3"
        audio_wav = workdir / f"scene_{i:03}.wav"
        image_path = workdir / f"scene_{i:03}.jpg"
        rhubarb_json = workdir / f"scene_{i:03}_mouth.json"
        clip_path = workdir / f"scene_{i:03}.mp4"

        print("  -> generating narration (edge-tts)")
        synthesize_narration(scene["narration"], voice, audio_mp3)
        duration = get_duration(audio_mp3) + 0.3
        mp3_to_wav(audio_mp3, audio_wav)

        print("  -> computing lip-sync (Rhubarb)")
        mouth_cues = run_rhubarb(audio_wav, rhubarb_json, args.rhubarb_bin)
        if mouth_cues is None:
            mouth_cues = approximate_mouth_cues(audio_wav, duration)
        blink_schedule = make_blink_schedule(duration)

        print(f"  -> generating background: {scene['image_prompt'][:70]}...")
        fetch_image(scene["image_prompt"], image_path, CANVAS_W, CANVAS_H)
        background = prepare_background(image_path, CANVAS_W, CANVAS_H)

        print(f"  -> rendering animated scene ({duration:.1f}s, {int(duration*FPS)} frames)")
        render_scene(background, rig, mouth_cues, blink_schedule, duration, audio_mp3,
                     clip_path, char_scale=args.character_scale)
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
