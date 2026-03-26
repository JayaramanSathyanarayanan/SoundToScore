"""
services/instrument.py
MIDI → rendered audio via FluidSynth + SoundFont (.sf2) files.

SoundFont download guide is in DEPLOYMENT_GUIDE.md.
All .sf2 files go in:  backend/soundfonts/
"""

import subprocess, os
from pathlib import Path

# ── SoundFont mapping ─────────────────────────────────────────
# Map instrument ID → .sf2 filename in the soundfonts/ folder.
# If a specific .sf2 doesn't exist, falls back to GeneralUser_GS.sf2
# which covers all General MIDI programs.

SF_DIR = Path(__file__).parent.parent / "soundfonts"

SOUNDFONTS: dict[str, str] = {
    # ── Brass ─────────────────────────────────────────────────
    "trumpet":          "trumpet.sf2",
    "flute":            "flute.sf2",
    "soprano_cornet":   "cornet.sf2",
    "solo_cornet":      "cornet.sf2",
    "repiano_cornet":   "cornet.sf2",
    "cornet_2nd":       "cornet.sf2",
    "cornet_3rd":       "cornet.sf2",
    "flugelhorn":       "flugelhorn.sf2",
    "solo_tenor_horn":  "tenor_horn.sf2",
    "tenor_horn_1st":   "tenor_horn.sf2",
    "tenor_horn_2nd":   "tenor_horn.sf2",
    "baritone_1st":     "baritone.sf2",
    "baritone_2nd":     "baritone.sf2",
    "trombone_1st":     "trombone.sf2",
    "trombone_2nd":     "trombone.sf2",
    "bass_trombone":    "bass_trombone.sf2",
    "euphonium":        "euphonium.sf2",
    "eb_bass":          "tuba.sf2",
    "bbb_bass":         "tuba.sf2",
    # ── Percussion ────────────────────────────────────────────
    "timpani":          "timpani.sf2",
    "drum_kit":         "drums.sf2",
    "glockenspiel":     "glockenspiel.sf2",
    "xylophone":        "xylophone.sf2",
    "tubular_bells":    "tubular_bells.sf2",
    "snare_drum":       "snare.sf2",
    "bass_drum":        "bass_drum.sf2",
    "cymbals":          "cymbals.sf2",
    "triangle":         "triangle.sf2",
    "tambourine":       "tambourine.sf2",
}

FALLBACK_SF2 = SF_DIR / "GeneralUser_GS.sf2"


def _resolve_sf2(instrument: str) -> str:
    """Return the best available .sf2 path for the instrument."""
    specific = SF_DIR / SOUNDFONTS.get(instrument, "")
    if specific.exists():
        return str(specific)
    if FALLBACK_SF2.exists():
        return str(FALLBACK_SF2)
    raise FileNotFoundError(
        f"No SoundFont found for '{instrument}' and no GeneralUser_GS.sf2 fallback. "
        "Download it from: https://musical-artifacts.com/artifacts/153"
    )


def midi_to_audio(midi_path: str, output_path: str, instrument: str = "solo_cornet", fmt: str = "wav") -> None:
    """
    Render a MIDI file to audio using FluidSynth.

    Args:
        midi_path:   Path to .mid file
        output_path: Path to write rendered audio
        instrument:  Instrument ID from SOUNDFONTS map
        fmt:         Output format — 'wav', 'mp3', or 'flac'
    """
    if not Path(midi_path).exists():
        raise FileNotFoundError(f"MIDI not found: {midi_path}")

    sf2 = _resolve_sf2(instrument)

    # FluidSynth always outputs WAV; we convert to mp3/flac with ffmpeg after
    tmp_wav = output_path if fmt == "wav" else output_path + "_tmp.wav"

    cmd = [
        "fluidsynth",
        "-ni",          # non-interactive
        "-g", "1.2",    # gain (louder than default)
        "-r", "44100",  # sample rate
        "-F", tmp_wav,  # output WAV
        sf2,
        midi_path,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"FluidSynth failed:\n{result.stderr[-800:]}")

    if not Path(tmp_wav).exists():
        raise FileNotFoundError("FluidSynth produced no output")

    # Convert format if needed
    if fmt == "mp3":
        _convert_format(tmp_wav, output_path, "libmp3lame", "mp3")
        Path(tmp_wav).unlink(missing_ok=True)
    elif fmt == "flac":
        _convert_format(tmp_wav, output_path, "flac", "flac")
        Path(tmp_wav).unlink(missing_ok=True)


def _convert_format(src: str, dst: str, codec: str, fmt: str) -> None:
    cmd = ["ffmpeg", "-y", "-i", src, "-acodec", codec, dst]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"Format conversion failed:\n{result.stderr[-400:]}")
