"""
services/instrument.py
MIDI to WAV using FluidSynth. SoundFont downloaded at build time via nixpacks.toml.
Has multiple fallback download attempts if build-time download failed.
"""
import subprocess
import logging
import urllib.request
import urllib.error
from pathlib import Path

log = logging.getLogger("soundtoscore")

# SoundFont path — relative to where uvicorn runs (backend/)
SF_DIR  = Path("soundfonts")
SF_FILE = SF_DIR / "GeneralUser_GS.sf2"
SF_DIR.mkdir(exist_ok=True)

SF_MIRRORS = [
    "https://drive.google.com/uc?export=download&id=1nVv5lw1vriViTil7ywpzOITUjiz-wlGh"
]

MIN_SF_BYTES = 5 * 1024 * 1024  # valid SF2 > 5MB


def _sf_valid() -> bool:
    return SF_FILE.exists() and SF_FILE.stat().st_size > MIN_SF_BYTES


def ensure_soundfont() -> str:
    if _sf_valid():
        return str(SF_FILE)

    log.warning("SoundFont missing — downloading from Google Drive...")
    for url in SF_MIRRORS:
        try:
            log.info(f"Trying: {url}")
            req = urllib.request.Request(url, headers={"User-Agent": "SoundToScore/2.0"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = resp.read()
            if len(data) > MIN_SF_BYTES:
                SF_FILE.write_bytes(data)
                log.info(f"SoundFont saved: {len(data)/1024/1024:.1f} MB")
                return str(SF_FILE)
            else:
                log.warning(f"Download too small ({len(data)} bytes), skipping")
        except Exception as e:
            log.warning(f"Download failed ({url}): {e}")

    raise RuntimeError(
        "SoundFont unavailable. Could not download GeneralUser_GS.sf2. "
        "Please add it manually to the soundfonts/ folder on the server."
    )


def midi_to_audio(midi_path: str, output_path: str,
                  instrument: str = "solo_cornet", fmt: str = "wav") -> None:
    if not Path(midi_path).exists():
        raise FileNotFoundError(f"MIDI not found: {midi_path}")

    sf2 = ensure_soundfont()
    tmp  = output_path if fmt == "wav" else output_path + "_tmp.wav"

    cmd = ["fluidsynth", "-ni", "-g", "1.4", "-r", "44100", "-F", tmp, sf2, midi_path]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"FluidSynth: {r.stderr[-400:]}")
    if not Path(tmp).exists():
        raise FileNotFoundError("FluidSynth produced no output file")

    if fmt == "mp3":
        cmd2 = ["ffmpeg", "-y", "-i", tmp, "-acodec", "libmp3lame", "-q:a", "4", output_path]
        r2 = subprocess.run(cmd2, capture_output=True, text=True, timeout=60)
        Path(tmp).unlink(missing_ok=True)
        if r2.returncode != 0:
            raise RuntimeError(f"MP3 conversion: {r2.stderr[-200:]}")
