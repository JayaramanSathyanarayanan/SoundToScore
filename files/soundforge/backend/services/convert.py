"""
services/convert.py
Audio → WAV conversion using FFmpeg.
"""

import subprocess, os
from pathlib import Path


def mp3_to_wav(input_path: str, output_path: str) -> None:
    """
    Convert any audio format to WAV 44100 Hz, mono, 16-bit.
    Mono is preferred because Basic Pitch performs better on mono audio.
    """
    if not Path(input_path).exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    cmd = [
        "ffmpeg", "-y",
        "-i",           input_path,
        "-ar",          "44100",      # sample rate
        "-ac",          "1",          # mono
        "-sample_fmt",  "s16",        # 16-bit
        "-vn",                        # no video
        output_path,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg failed:\n{result.stderr[-800:]}")

    if not Path(output_path).exists():
        raise FileNotFoundError("FFmpeg produced no output file")
