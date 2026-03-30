"""
services/instrument.py
MIDI -> audio via FluidSynth.
SoundFont: downloaded from Google Drive at startup.
Place your GeneralUser_GS.sf2 on Google Drive, share as "Anyone with link can view",
then paste the file ID below.
"""
import subprocess, logging, urllib.request
from pathlib import Path

log = logging.getLogger("soundtoscore")

SF_DIR  = Path("soundfonts")
SF_FILE = SF_DIR / "GeneralUser_GS.sf2"
SF_DIR.mkdir(exist_ok=True)

# ── YOUR GOOGLE DRIVE FILE ID ─────────────────────────────────
# Get this from your Drive share link:
# https://drive.google.com/file/d/XXXXX/view  → XXXXX is the ID
GDRIVE_FILE_ID = "1nVv5lw1vriViTil7ywpzOITUjiz-wlGh"

# Fallback public mirrors if Drive fails
SF_MIRRORS = [
    f"https://drive.google.com/uc?export=download&id={GDRIVE_FILE_ID}&confirm=t",
    f"https://docs.google.com/uc?export=download&id={GDRIVE_FILE_ID}",
    "https://github.com/fkarg/GeneralUserGS/releases/download/v1.471/GeneralUser_GS_v1.471.sf2",
]

MIN_SIZE = 5 * 1024 * 1024


def _valid() -> bool:
    return SF_FILE.exists() and SF_FILE.stat().st_size > MIN_SIZE


def ensure_soundfont() -> str:
    if _valid():
        return str(SF_FILE)

    log.warning("SoundFont not found — downloading...")
    for url in SF_MIRRORS:
        try:
            log.info(f"Trying: {url[:60]}...")
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 SoundToScore/3.0",
                    "Accept": "*/*",
                }
            )
            with urllib.request.urlopen(req, timeout=180) as r:
                # Handle Google Drive large-file confirmation
                content_type = r.headers.get("Content-Type", "")
                raw = r.read()

            # If Google returned an HTML confirmation page, skip
            if b"<!DOCTYPE" in raw[:100] or b"<html" in raw[:100]:
                log.warning("Got HTML instead of SF2 (Drive confirmation page) — trying next")
                continue

            if len(raw) > MIN_SIZE:
                SF_FILE.write_bytes(raw)
                log.info(f"SoundFont saved: {len(raw)//1024//1024} MB")
                return str(SF_FILE)
            log.warning(f"File too small ({len(raw)} bytes)")
        except Exception as e:
            log.warning(f"Mirror failed: {e}")

    raise RuntimeError(
        "Could not download SoundFont. "
        "Please upload GeneralUser_GS.sf2 via Render Shell: "
        "mkdir -p soundfonts && curl -L YOUR_URL -o soundfonts/GeneralUser_GS.sf2"
    )


def midi_to_audio(midi_path: str, output_path: str,
                  instrument: str = "solo_cornet", fmt: str = "wav") -> None:
    if not Path(midi_path).exists():
        raise FileNotFoundError(f"MIDI not found: {midi_path}")

    sf2 = ensure_soundfont()
    tmp = output_path if fmt == "wav" else output_path + "_tmp.wav"

    r = subprocess.run(
        ["fluidsynth", "-ni", "-g", "1.4", "-r", "44100", "-F", tmp, sf2, midi_path],
        capture_output=True, text=True, timeout=120
    )
    if r.returncode != 0:
        raise RuntimeError(f"FluidSynth: {r.stderr[-400:]}")
    if not Path(tmp).exists():
        raise FileNotFoundError("FluidSynth produced no output")

    if fmt == "mp3":
        r2 = subprocess.run(
            ["ffmpeg", "-y", "-i", tmp, "-acodec", "libmp3lame", "-q:a", "4", output_path],
            capture_output=True, text=True, timeout=60
        )
        Path(tmp).unlink(missing_ok=True)
        if r2.returncode != 0:
            raise RuntimeError(f"MP3 convert: {r2.stderr[-200:]}")
