from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

SIDE = 72
PHOTO_SIGNATURES = ((b"\xff\xd8\xff", "jpeg"), (b"\x89PNG\r\n\x1a\n", "png"))


def photo_input(source: str) -> str | None:
    """`source` with its decoder named for ImageMagick, or None if it is not a JPEG, PNG or WebP.

    Left to itself ImageMagick picks a decoder from the content, and some (SVG, MVG, MSL) read other
    files or run programs. These files come from other people's messages.
    """
    try:
        with open(source, "rb") as f:
            head = f.read(12)
    except OSError:
        return None
    coder = next((name for magic, name in PHOTO_SIGNATURES if head.startswith(magic)), None)
    if head[:4] == b"RIFF" and head[8:] == b"WEBP":
        coder = "webp"
    return f"{coder}:{source}[0]" if coder else None


def dimensions(source: str) -> tuple[int, int] | None:
    if shutil.which("sips"):
        out = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", source],
                             capture_output=True, text=True).stdout
        width, height = re.search(r"pixelWidth: (\d+)", out), re.search(r"pixelHeight: (\d+)", out)
        return (int(width.group(1)), int(height.group(1))) if width and height else None
    if (magick := shutil.which("magick")) and (photo := photo_input(source)):
        out = subprocess.run([magick, "identify", "-format", "%w %h", photo], capture_output=True, text=True).stdout
        match = re.fullmatch(r"(\d+) (\d+)", out.strip())
        return (int(match.group(1)), int(match.group(2))) if match else None
    return None


def image(source: str, target: Path) -> tuple[int, int] | None:
    size = dimensions(source)
    if not size:
        return None
    side = str(min(size))
    if shutil.which("sips"):
        steps = (["sips", "-s", "format", "jpeg", "-c", side, side, source, "--out", str(target)],
                 ["sips", "-z", str(SIDE), str(SIDE), str(target)])
    else:
        steps = ([shutil.which("magick"), photo_input(source), "-gravity", "center", "-crop", f"{side}x{side}+0+0",
                  "+repage", "-resize", f"{SIDE}x{SIDE}!", f"jpeg:{target}"],)
    for step in steps:
        if subprocess.run(step, capture_output=True).returncode:
            target.unlink(missing_ok=True)
            return None
    return size if target.is_file() else None


def video(source: str, target: Path) -> bool:
    ffmpeg = shutil.which("ffmpeg") or shutil.which("/opt/homebrew/bin/ffmpeg")
    if not ffmpeg:
        return False
    done = subprocess.run([ffmpeg, "-v", "error", "-y", "-i", source, "-frames:v", "1", "-vf",
                           f"crop=min(iw\\,ih):min(iw\\,ih),scale={SIDE}:{SIDE}", "-f", "image2", "-c:v", "mjpeg",
                           "-q:v", "4", str(target)], capture_output=True)
    return done.returncode == 0 and target.is_file()
