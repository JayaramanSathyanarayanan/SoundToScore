"""
services/midi.py
WAV to MIDI using librosa + pretty_midi (lightweight, no TensorFlow, uses ~50MB RAM)
Much more memory efficient than Basic Pitch for free hosting tiers.
"""

import subprocess
import numpy as np
from pathlib import Path
import pretty_midi


def wav_to_midi(wav_path: str, midi_path: str) -> None:
    """
    Convert WAV to MIDI using aubio (pitch detection via command line).
    Falls back to librosa if aubio is not available.
    Uses ~50MB RAM vs Basic Pitch which uses ~500MB.
    """
    if not Path(wav_path).exists():
        raise FileNotFoundError(f"WAV not found: {wav_path}")

    # Try aubio first (very lightweight)
    try:
        _convert_with_aubio(wav_path, midi_path)
        return
    except Exception as e:
        pass

    # Fallback: librosa pitch tracking
    try:
        _convert_with_librosa(wav_path, midi_path)
        return
    except Exception as e:
        raise RuntimeError(f"MIDI conversion failed: {e}")


def _convert_with_aubio(wav_path: str, midi_path: str) -> None:
    """Use aubio command line tool for pitch detection."""
    import tempfile
    import os

    # aubio outputs pitch data as text
    result = subprocess.run(
        ["aubio", "pitch", "-i", wav_path, "-r", "44100", "-H", "512"],
        capture_output=True, text=True, timeout=120
    )

    if result.returncode != 0:
        raise RuntimeError("aubio failed")

    lines = result.stdout.strip().split("\n")
    if not lines:
        raise RuntimeError("No pitch data from aubio")

    # Parse aubio output: "timestamp  frequency  confidence"
    pm = pretty_midi.PrettyMIDI(initial_tempo=120)
    instrument = pretty_midi.Instrument(program=0)

    notes_data = []
    for line in lines:
        parts = line.strip().split()
        if len(parts) >= 2:
            try:
                t = float(parts[0])
                freq = float(parts[1])
                if freq > 30:  # valid pitch
                    midi_num = int(pretty_midi.hz_to_note_number(freq))
                    if 21 <= midi_num <= 108:
                        notes_data.append((t, midi_num))
            except (ValueError, OverflowError):
                continue

    # Group consecutive same notes
    if notes_data:
        notes = _group_notes(notes_data, hop_size=512/44100)
        instrument.notes = notes

    pm.instruments.append(instrument)
    pm.write(midi_path)

    if not Path(midi_path).exists():
        raise FileNotFoundError("MIDI file not created")


def _convert_with_librosa(wav_path: str, midi_path: str) -> None:
    """Use librosa for pitch tracking - lightweight alternative."""
    import librosa

    # Load audio (mono, 22050 Hz to save memory)
    y, sr = librosa.load(wav_path, sr=22050, mono=True)

    # Pitch tracking using pyin (accurate, low RAM)
    f0, voiced_flag, voiced_probs = librosa.pyin(
        y,
        fmin=librosa.note_to_hz("C2"),
        fmax=librosa.note_to_hz("C7"),
        sr=sr,
        frame_length=2048,
        hop_length=512,
    )

    hop_duration = 512 / sr
    times = np.arange(len(f0)) * hop_duration

    # Build note events
    notes_data = []
    for t, freq, voiced in zip(times, f0, voiced_flag):
        if voiced and freq and not np.isnan(freq) and freq > 30:
            try:
                midi_num = int(round(librosa.hz_to_midi(freq)))
                if 21 <= midi_num <= 108:
                    notes_data.append((t, midi_num))
            except Exception:
                continue

    # Create MIDI
    pm = pretty_midi.PrettyMIDI(initial_tempo=120)
    instrument = pretty_midi.Instrument(program=0)

    if notes_data:
        instrument.notes = _group_notes(notes_data, hop_size=hop_duration)

    pm.instruments.append(instrument)
    pm.write(midi_path)

    if not Path(midi_path).exists():
        raise FileNotFoundError("MIDI file not created")

    # Free memory
    del y


def _group_notes(notes_data, hop_size=0.01162, min_duration=0.05):
    """Group consecutive pitch detections into MIDI notes."""
    notes = []
    if not notes_data:
        return notes

    start_time, current_pitch = notes_data[0]
    last_time = start_time

    for t, pitch in notes_data[1:]:
        if pitch == current_pitch and (t - last_time) < hop_size * 3:
            last_time = t
        else:
            duration = last_time - start_time + hop_size
            if duration >= min_duration:
                note = pretty_midi.Note(
                    velocity=80,
                    pitch=current_pitch,
                    start=start_time,
                    end=start_time + duration
                )
                notes.append(note)
            start_time = t
            current_pitch = pitch
            last_time = t

    # Last note
    duration = last_time - start_time + hop_size
    if duration >= min_duration:
        note = pretty_midi.Note(
            velocity=80,
            pitch=current_pitch,
            start=start_time,
            end=start_time + duration
        )
        notes.append(note)

    return notes

