"""
services/transcript.py
Generate readable note transcript from MIDI.
Returns transcript text AND writes to file.
"""

from pathlib import Path
import pretty_midi

NOTE_NAMES = ['C','C#','D','D#','E','F','F#','G','G#','A','A#','B']


def _note_name(midi_num: int) -> str:
    octave = (midi_num // 12) - 1
    return f"{NOTE_NAMES[midi_num % 12]}{octave}"


def generate_transcript(midi_path: str, output_txt: str, tempo: int = 120) -> str:
    if not Path(midi_path).exists():
        return ""

    try:
        pm = pretty_midi.PrettyMIDI(midi_path)
        lines = []
        for instrument in pm.instruments:
            for note in sorted(instrument.notes, key=lambda n: n.start):
                name = _note_name(note.pitch)
                lines.append(
                    f"{name} vel:{note.velocity} t={note.start:.3f}s dur={note.end-note.start:.3f}s"
                )
        text = "\n".join(lines)
        Path(output_txt).write_text(text or "No notes detected", encoding="utf-8")
        return text
    except Exception as e:
        Path(output_txt).write_text(f"Transcript error: {e}", encoding="utf-8")
        return ""
