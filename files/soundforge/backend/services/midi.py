"""
services/midi.py
WAV → MIDI using Spotify Basic Pitch AI model.
"""

from pathlib import Path


def wav_to_midi(wav_path: str, midi_path: str) -> None:
    """
    Convert a WAV file to MIDI using Basic Pitch.

    Tuned parameters for brass band music:
    - onset_threshold: 0.5   (how confident the model must be about a note start)
    - frame_threshold: 0.3   (how confident the model must be about a note continuing)
    - minimum_note_length: 58 ms  (ignore very short blips)
    - frequency range: C1–C7  (covers all brass + percussion pitched range)
    """
    if not Path(wav_path).exists():
        raise FileNotFoundError(f"WAV not found: {wav_path}")

    # Import here so startup is fast even if basic-pitch is slow to load
    from basic_pitch.inference import predict
    from basic_pitch import ICASSP_2022_MODEL_PATH

    _, midi_data, _ = predict(
        wav_path,
        ICASSP_2022_MODEL_PATH,
        onset_threshold=0.5,
        frame_threshold=0.3,
        minimum_note_length=58,
        minimum_frequency=32.7,    # C1
        maximum_frequency=2093.0,  # C7
        melodia_trick=True,        # improves melody extraction
        multiple_pitch_bends=False,
    )

    midi_data.write(midi_path)

    if not Path(midi_path).exists():
        raise FileNotFoundError("Basic Pitch produced no MIDI output")
