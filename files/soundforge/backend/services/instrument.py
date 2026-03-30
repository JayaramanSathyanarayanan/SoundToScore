"""
services/instrument.py — MIDI to audio via FluidSynth
SoundFont auto-downloaded at startup with 3 reliable mirrors.
"""
import subprocess, logging, urllib.request
from pathlib import Path

log = logging.getLogger("soundtoscore")

SF_DIR  = Path("soundfonts")
SF_FILE = SF_DIR / "GeneralUser_GS.sf2"
SF_DIR.mkdir(exist_ok=True)

# Three reliable mirrors — first GitHub raw, then others
SF_MIRRORS = [
    "https://github.com/fkarg/GeneralUserGS/releases/download/v1.471/GeneralUser_GS_v1.471.sf2",
    "https://github.com/mrbumpy409/GeneralUser-GS/raw/master/GeneralUser%20GS%20v1.471.sf2",
    "https://keymusician01.s3.amazonaws.com/GeneralUser_GS.sf2",
]
MIN_SIZE = 5 * 1024 * 1024   # >5 MB = valid


def _valid() -> bool:
    return SF_FILE.exists() and SF_FILE.stat().st_size > MIN_SIZE


def ensure_soundfont() -> str:
    if _valid():
        return str(SF_FILE)

    log.warning("SoundFont missing — downloading...")
    for url in SF_MIRRORS:
        try:
            log.info(f"Trying: {url}")
            req = urllib.request.Request(url, headers={"User-Agent": "SoundToScore/3.0"})
            with urllib.request.urlopen(req, timeout=120) as r:
                raw = r.read()
            if len(raw) > MIN_SIZE:
                SF_FILE.write_bytes(raw)
                log.info(f"SoundFont saved ({len(raw)//1024//1024} MB)")
                return str(SF_FILE)
            log.warning(f"Too small ({len(raw)} bytes), skipping")
        except Exception as e:
            log.warning(f"Mirror failed: {e}")

    raise RuntimeError(
        "Could not download SoundFont. "
        "Manually place GeneralUser_GS.sf2 in the soundfonts/ folder."
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
