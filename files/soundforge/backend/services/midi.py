"""
services/midi.py
Lightweight chunk-based WAV to MIDI using librosa YIN pitch detection.
Uses ~80MB RAM vs Basic Pitch 500MB. Processes one chunk at a time.
"""

import numpy as np
from pathlib import Path
import pretty_midi
import librosa


def wav_chunk_to_midi(wav_path: str, midi_path: str, sr: int = 16000, tempo: int = 120) -> None:
    if not Path(wav_path).exists():
        raise FileNotFoundError(f"WAV not found: {wav_path}")

    # Load chunk audio
    y, _ = librosa.load(wav_path, sr=sr, mono=True)

    # Normalize and preemphasis for better pitch detection
    y = librosa.util.normalize(y)
    y = librosa.effects.preemphasis(y)

    # YIN pitch detection — fast and lightweight
    f0 = librosa.yin(
        y,
        fmin=50,
        fmax=2000,
        sr=sr,
        frame_length=2048,
        hop_length=512,
    )

    hop_duration = 512 / sr
    times = np.arange(len(f0)) * hop_duration

    # Build note events from pitch contour
    notes_data = []
    for t, freq in zip(times, f0):
        if freq > 30 and not np.isnan(freq) and not np.isinf(freq):
            try:
                midi_num = int(round(librosa.hz_to_midi(freq)))
                if 21 <= midi_num <= 108:
                    notes_data.append((float(t), int(midi_num)))
            except Exception:
                continue

    # Create MIDI file
    pm = pretty_midi.PrettyMIDI(initial_tempo=float(tempo))
    instrument = pretty_midi.Instrument(program=0)
    instrument.notes = _group_notes(notes_data, hop_size=hop_duration)
    pm.instruments.append(instrument)
    pm.write(midi_path)

    # Free memory
    del y, f0


def _group_notes(notes_data, hop_size=0.032, min_duration=0.05):
    notes = []
    if not notes_data:
        return notes

    start_time, current_pitch = notes_data[0]
    last_time = start_time

    for t, pitch in notes_data[1:]:
        if pitch == current_pitch and (t - last_time) < hop_size * 4:
            last_time = t
        else:
            duration = last_time - start_time + hop_size
            if duration >= min_duration:
                notes.append(pretty_midi.Note(
                    velocity=80,
                    pitch=current_pitch,
                    start=start_time,
                    end=start_time + duration
                ))
            start_time = t
            current_pitch = pitch
            last_time = t

    # Last note
    duration = last_time - start_time + hop_size
    if duration >= min_duration:
        notes.append(pretty_midi.Note(
            velocity=80,
            pitch=current_pitch,
            start=start_time,
            end=start_time + duration
        ))

    return notes
