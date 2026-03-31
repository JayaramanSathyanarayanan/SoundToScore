"""
services/instrument.py
MIDI -> audio via FluidSynth.
SoundFont: downloaded from Google Drive at startup.
Place your GeneralUser_GS.sf2 on Google Drive, share as "Anyone with link can view",
then paste the file ID below.
"""
import subprocess, logging, shutil
from pathlib import Path
import requests

log = logging.getLogger("soundtoscore")

SF_DIR  = Path("soundfonts")
SF_FILE = SF_DIR / "GeneralUser_GS.sf2"
SF_DIR.mkdir(exist_ok=True)

SF_URL = "https://github.com/JayaramanSathyanarayanan/SoundToScore/releases/download/v1.0/GeneralUser_GS.sf2"

MIN_SIZE = 5 * 1024 * 1024


def _valid() -> bool:
    return SF_FILE.exists() and SF_FILE.stat().st_size > MIN_SIZE


def ensure_soundfont() -> str:
    if _valid():
        log.info("✅ SoundFont already exists")
        return str(SF_FILE)
        
    tmp_file = SF_FILE.with_suffix(".tmp")
    if tmp_file.exists():
        tmp_file.unlink(missing_ok=True)
    log.warning("SoundFont not found — downloading from GitHub Release...")

    try:
        tmp_file = SF_FILE.with_suffix(".tmp")

        with requests.get(SF_URL, stream=True, timeout=180) as r:
            r.raise_for_status()

            with open(tmp_file, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)

        tmp_file.replace(SF_FILE)  # ✅ atomic replace

        if _valid():
            log.info(f"✅ SoundFont downloaded: {SF_FILE.stat().st_size//1024//1024} MB")
            return str(SF_FILE)

        raise RuntimeError("Downloaded file invalid")

    except Exception as e:
        if SF_FILE.exists():
            SF_FILE.unlink(missing_ok=True)
        raise RuntimeError(f"SoundFont download failed: {e}")

def midi_to_audio(midi_path: str, output_path: str,
                  instrument: str = "solo_cornet", fmt: str = "wav") -> None:
    if not Path(midi_path).exists():
        raise FileNotFoundError(f"MIDI not found: {midi_path}")
    # 🔥 CHECK fluidsynth HERE (correct place)
    if not shutil.which("fluidsynth"):
        raise RuntimeError("fluidsynth not installed in environment")

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
