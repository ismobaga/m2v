#!/usr/bin/env python3
import argparse
import asyncio
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import List, Optional

from PIL import Image

try:
    import edge_tts
except ImportError:
    edge_tts = None


IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def die(msg: str, code: int = 1) -> None:
    print(f"[ERROR] {msg}", file=sys.stderr)
    sys.exit(code)


def run(cmd: List[str]) -> None:
    print("[CMD]", " ".join(cmd))
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if p.returncode != 0:
        print(p.stdout)
        die(f"Command failed: {' '.join(cmd)}")


def check_ffmpeg() -> None:
    try:
        subprocess.run(["ffmpeg", "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    except Exception:
        die("ffmpeg not found. Install ffmpeg and make sure it's in PATH.")


def natural_sort_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def collect_images_from_dir(folder: Path) -> List[Path]:
    if not folder.exists() or not folder.is_dir():
        die(f"Input folder not found: {folder}")
    imgs = [p for p in folder.iterdir() if p.suffix.lower() in IMG_EXTS and p.is_file()]
    imgs.sort(key=lambda p: natural_sort_key(p.name))
    if not imgs:
        die(f"No images found in folder: {folder}")
    return imgs


def extract_cbz(cbz_path: Path, out_dir: Path) -> List[Path]:
    if not cbz_path.exists() or not cbz_path.is_file():
        die(f"CBZ not found: {cbz_path}")
    with zipfile.ZipFile(cbz_path, "r") as z:
        z.extractall(out_dir)
    # collect images recursively
    imgs = []
    for p in out_dir.rglob("*"):
        if p.is_file() and p.suffix.lower() in IMG_EXTS:
            imgs.append(p)
    imgs.sort(key=lambda p: natural_sort_key(p.name))
    if not imgs:
        die(f"No images found inside CBZ: {cbz_path}")
    return imgs


def ensure_even_dimensions(img_path: Path) -> None:
    # Some encoders dislike odd dimensions; ensure width/height are even
    with Image.open(img_path) as im:
        w, h = im.size
        nw, nh = (w // 2) * 2, (h // 2) * 2
        if (nw, nh) != (w, h):
            im = im.crop((0, 0, nw, nh))
            im.save(img_path)


def make_frames(
    images: List[Path],
    frames_dir: Path,
    target_w: int,
    target_h: int,
    fit: str = "contain",
) -> None:
    """
    Convert images to uniform PNG frames at target resolution.
    fit:
      - contain: letterbox (no crop)
      - cover: crop to fill
    """
    frames_dir.mkdir(parents=True, exist_ok=True)

    for i, img in enumerate(images, start=1):
        out = frames_dir / f"frame_{i:05d}.png"
        with Image.open(img) as im:
            im = im.convert("RGB")
            w, h = im.size

            if fit == "cover":
                # scale then crop to fill
                scale = max(target_w / w, target_h / h)
                sw, sh = int(w * scale), int(h * scale)
                im = im.resize((sw, sh), Image.LANCZOS)
                left = (sw - target_w) // 2
                top = (sh - target_h) // 2
                im = im.crop((left, top, left + target_w, top + target_h))
            else:
                # contain: scale then paste on black canvas
                scale = min(target_w / w, target_h / h)
                sw, sh = int(w * scale), int(h * scale)
                im_resized = im.resize((sw, sh), Image.LANCZOS)
                canvas = Image.new("RGB", (target_w, target_h), (0, 0, 0))
                left = (target_w - sw) // 2
                top = (target_h - sh) // 2
                canvas.paste(im_resized, (left, top))
                im = canvas

            # ensure even dims
            if (target_w % 2) or (target_h % 2):
                die("Target width/height must be even numbers.")
            im.save(out, "PNG")

        ensure_even_dimensions(out)

    print(f"[OK] Frames created: {len(images)} → {frames_dir}")


async def synth_tts_edge(text: str, out_mp3: Path, voice: str, rate: str = "+0%") -> None:
    if edge_tts is None:
        die("edge-tts is not installed. Run: pip install edge-tts")
    communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate)
    await communicate.save(str(out_mp3))


def read_script(script_path: Optional[Path], default_text: str) -> str:
    if script_path is None:
        return default_text
    if not script_path.exists():
        die(f"Script file not found: {script_path}")
    txt = script_path.read_text(encoding="utf-8").strip()
    if not txt:
        die("Script file is empty.")
    return txt


def get_audio_duration_sec(audio_path: Path) -> float:
    # Use ffprobe to get duration
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(audio_path),
    ]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        die(f"ffprobe failed:\n{p.stderr}")
    try:
        return float(p.stdout.strip())
    except ValueError:
        die("Could not parse audio duration from ffprobe output.")


def build_video_with_kenburns(
    frames_dir: Path,
    audio_mp3: Path,
    out_mp4: Path,
    fps: int,
    seconds_per_image: float,
    zoom: float,
    pan: str,
    crf: int,
) -> None:
    """
    Uses ffmpeg zoompan to animate each image, concatenated from frames.
    We first generate a video from frames with zoompan, then mux audio.
    """
    temp_video = out_mp4.with_suffix(".silent.mp4")

    # zoompan basics:
    # - Each input frame is 1 image; we use -framerate 1/seconds_per_image by repeating frames.
    # Alternative: treat frames as an image sequence at fps and use zoompan with d=... (frames per image).
    d = max(1, int(round(seconds_per_image * fps)))

    # Pan direction presets
    # x/y expressions reference "iw/ih" and "zoom"
    if pan == "left":
        x_expr = "iw/2-(iw/zoom)/2 - (iw/zoom)*0.10*(on/d)"
        y_expr = "ih/2-(ih/zoom)/2"
    elif pan == "right":
        x_expr = "iw/2-(iw/zoom)/2 + (iw/zoom)*0.10*(on/d)"
        y_expr = "ih/2-(ih/zoom)/2"
    elif pan == "up":
        x_expr = "iw/2-(iw/zoom)/2"
        y_expr = "ih/2-(ih/zoom)/2 - (ih/zoom)*0.10*(on/d)"
    elif pan == "down":
        x_expr = "iw/2-(iw/zoom)/2"
        y_expr = "ih/2-(ih/zoom)/2 + (ih/zoom)*0.10*(on/d)"
    else:
        x_expr = "iw/2-(iw/zoom)/2"
        y_expr = "ih/2-(ih/zoom)/2"

    # Zoom expression: gently increase until target zoom, then hold
    # on is the output frame number for the current input image inside zoompan
    z_expr = f"if(lte(on, {d}), 1+({zoom}-1)*on/{d}, {zoom})"

    vf = (
        f"zoompan=z='{z_expr}':x='{x_expr}':y='{y_expr}':d={d}:fps={fps},"
        f"format=yuv420p"
    )

    run([
        "ffmpeg", "-y",
        "-framerate", str(fps),
        "-i", str(frames_dir / "frame_%05d.png"),
        "-vf", vf,
        "-c:v", "libx264",
        "-crf", str(crf),
        "-preset", "medium",
        str(temp_video),
    ])

    # Mux audio (shortest to end with audio)
    run([
        "ffmpeg", "-y",
        "-i", str(temp_video),
        "-i", str(audio_mp3),
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",
        str(out_mp4),
    ])

    temp_video.unlink(missing_ok=True)
    print(f"[OK] Video created: {out_mp4}")


def main():
    parser = argparse.ArgumentParser(description="Manga/Comic → Video MVP (images/CBZ → TTS → MP4)")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--images", type=str, help="Input folder containing images (jpg/png/webp)")
    src.add_argument("--cbz", type=str, help="Input .cbz file")

    parser.add_argument("--script", type=str, default=None, help="Text file for narration (UTF-8). If omitted, a default narration is used.")
    parser.add_argument("--out", type=str, default="output.mp4", help="Output MP4 path")
    parser.add_argument("--voice", type=str, default="en-US-AriaNeural", help="Edge TTS voice, e.g. en-US-AriaNeural, fr-FR-DeniseNeural")
    parser.add_argument("--rate", type=str, default="+0%", help="TTS rate, e.g. -10%, +10%")
    parser.add_argument("--fps", type=int, default=30, help="Video FPS")
    parser.add_argument("--seconds-per-image", type=float, default=3.0, help="Seconds each page stays on screen")
    parser.add_argument("--w", type=int, default=1080, help="Target width (even number recommended)")
    parser.add_argument("--h", type=int, default=1920, help="Target height (even number recommended)")
    parser.add_argument("--fit", choices=["contain", "cover"], default="contain", help="Resize behavior: contain(letterbox) or cover(crop)")
    parser.add_argument("--zoom", type=float, default=1.12, help="Target zoom factor per image (e.g. 1.08 to 1.20)")
    parser.add_argument("--pan", choices=["center", "left", "right", "up", "down"], default="center", help="Pan direction")
    parser.add_argument("--crf", type=int, default=20, help="x264 quality (lower=better, typical 18-23)")
    args = parser.parse_args()

    check_ffmpeg()

    out_mp4 = Path(args.out).resolve()
    out_mp4.parent.mkdir(parents=True, exist_ok=True)

    default_narration = (
        "Welcome. This is an automated manga to video demo. "
        "Replace this narration with your own script file for better results."
    )
    text = read_script(Path(args.script) if args.script else None, default_narration)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        work_dir = tmp_dir / "work"
        work_dir.mkdir()

        # Load images
        if args.cbz:
            imgs = extract_cbz(Path(args.cbz).resolve(), work_dir / "cbz_extract")
        else:
            imgs = collect_images_from_dir(Path(args.images).resolve())

        frames_dir = work_dir / "frames"
        make_frames(imgs, frames_dir, args.w, args.h, fit=args.fit)

        # TTS
        audio_mp3 = work_dir / "narration.mp3"
        print(f"[INFO] Generating TTS with voice={args.voice} rate={args.rate} ...")
        asyncio.run(synth_tts_edge(text=text, out_mp3=audio_mp3, voice=args.voice, rate=args.rate))
        print(f"[OK] Audio created: {audio_mp3}")

        # Optional: adapt seconds_per_image based on audio length if user wants auto pacing
        # Here we keep it simple: use provided seconds-per-image.

        build_video_with_kenburns(
            frames_dir=frames_dir,
            audio_mp3=audio_mp3,
            out_mp4=out_mp4,
            fps=args.fps,
            seconds_per_image=args.seconds_per_image,
            zoom=args.zoom,
            pan=args.pan,
            crf=args.crf,
        )


if __name__ == "__main__":
    main()
