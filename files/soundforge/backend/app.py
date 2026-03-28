import os
import uuid
import shutil
import time
import logging
import asyncio
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from services.convert import mp3_to_wav
from services.midi import wav_chunk_to_midi
from services.instrument import midi_to_audio
from services.transcript import generate_transcript

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("soundtoscore")

app = FastAPI(title="SoundToScore API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_DIR = Path("uploads")
OUTPUT_DIR = Path("outputs")
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

CHUNK_SECONDS = 20
MAX_BYTES = 100 * 1024 * 1024  # 100MB (browser compresses first)

VALID_INSTRUMENTS = {
    "trumpet","flute","soprano_cornet","solo_cornet","repiano_cornet",
    "cornet_2nd","cornet_3rd","flugelhorn","solo_tenor_horn","tenor_horn_1st",
    "tenor_horn_2nd","baritone_1st","baritone_2nd","trombone_1st","trombone_2nd",
    "bass_trombone","euphonium","eb_bass","bbb_bass",
    "timpani","drum_kit","glockenspiel","xylophone","tubular_bells",
    "snare_drum","bass_drum","cymbals","triangle","tambourine",
}


@app.get("/")
def health():
    return {"status": "SoundToScore is running", "version": "2.0.0"}


@app.get("/api/health")
def api_health():
    return {"ok": True}


@app.post("/api/convert")
async def convert(
    request: Request,
    background: BackgroundTasks,
    file: UploadFile = File(...),
    instrument: str = Form("solo_cornet"),
    tempo: int = Form(120),
    output_fmt: str = Form("wav"),
):
    if instrument not in VALID_INSTRUMENTS:
        raise HTTPException(400, f"Unknown instrument: {instrument}")

    original_name = file.filename or "upload"
    ext = Path(original_name).suffix.lower()
    if ext not in {".mp3", ".wav", ".m4a", ".ogg", ".flac"}:
        raise HTTPException(400, f"Unsupported file type: {ext}")

    data = await file.read()
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "File too large even after compression.")

    job_id = uuid.uuid4().hex[:10]
    job_dir = OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    src_path = UPLOAD_DIR / f"{job_id}_input{ext}"
    src_path.write_bytes(data)

    quality_warning = None
    file_kb = len(data) / 1024
    if file_kb < 500:
        quality_warning = "Low-quality audio detected. Accuracy may be reduced."

    log.info(f"[{job_id}] {len(data)/1024:.1f}KB instrument={instrument}")
    t0 = time.time()

    try:
        # Step 1: Convert to WAV
        wav_path = job_dir / "full_audio.wav"
        mp3_to_wav(str(src_path), str(wav_path))

        # Step 2: Split into chunks and process each
        import librosa
        import soundfile as sf
        import numpy as np

        y, sr = librosa.load(str(wav_path), sr=16000, mono=True)
        duration = len(y) / sr
        chunk_samples = CHUNK_SECONDS * sr
        total_chunks = max(1, int(np.ceil(len(y) / chunk_samples)))

        log.info(f"[{job_id}] Duration={duration:.1f}s chunks={total_chunks}")

        chunks_result = []

        for chunk_idx in range(total_chunks):
            start_sample = chunk_idx * chunk_samples
            end_sample = min(start_sample + chunk_samples, len(y))
            chunk_y = y[start_sample:end_sample]

            chunk_start_sec = chunk_idx * CHUNK_SECONDS
            chunk_end_sec = min(chunk_start_sec + CHUNK_SECONDS, duration)

            chunk_dir = job_dir / f"chunk_{chunk_idx+1}"
            chunk_dir.mkdir(exist_ok=True)

            # Save chunk WAV
            chunk_wav = chunk_dir / "audio.wav"
            sf.write(str(chunk_wav), chunk_y, sr)

            # WAV -> MIDI
            chunk_midi = chunk_dir / "output.mid"
            wav_chunk_to_midi(str(chunk_wav), str(chunk_midi), sr=sr, tempo=tempo)

            # MIDI -> Instrument audio
            rendered_name = f"output_{instrument}.{output_fmt}"
            rendered_path = chunk_dir / rendered_name
            midi_to_audio(str(chunk_midi), str(rendered_path), instrument=instrument, fmt=output_fmt)

            # Transcript
            transcript_path = chunk_dir / "notes.txt"
            transcript_text = generate_transcript(str(chunk_midi), str(transcript_path), tempo=tempo)

            base = f"/api/download/{job_id}/chunk_{chunk_idx+1}"
            chunks_result.append({
                "chunk": chunk_idx + 1,
                "start": round(chunk_start_sec, 1),
                "end": round(chunk_end_sec, 1),
                "audio": f"{base}/{rendered_name}",
                "midi": f"{base}/output.mid",
                "transcript": f"{base}/notes.txt",
                "transcript_preview": transcript_text[:300] if transcript_text else "",
            })

            # Free memory immediately
            del chunk_y
            log.info(f"[{job_id}] Chunk {chunk_idx+1}/{total_chunks} done")

        del y

    except Exception as exc:
        log.error(f"[{job_id}] Failed: {exc}")
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(500, f"Processing error: {exc}")
    finally:
        src_path.unlink(missing_ok=True)

    background.add_task(_cleanup, str(job_dir))

    return JSONResponse({
        "job_id": job_id,
        "status": "success",
        "elapsed": round(time.time() - t0, 2),
        "total_chunks": total_chunks,
        "duration": round(duration, 1),
        "quality_warning": quality_warning,
        "chunks": chunks_result,
    })


@app.get("/api/download/{job_id}/{chunk_folder}/{filename}")
def download_chunk(job_id: str, chunk_folder: str, filename: str):
    if not job_id.isalnum():
        raise HTTPException(400, "Invalid job ID")
    if ".." in filename or "/" in filename or ".." in chunk_folder:
        raise HTTPException(400, "Invalid path")
    path = OUTPUT_DIR / job_id / chunk_folder / filename
    if not path.exists():
        raise HTTPException(404, "File not found or expired")
    return FileResponse(str(path), filename=filename)


async def _cleanup(job_dir: str, delay: int = 7200):
    await asyncio.sleep(delay)
    shutil.rmtree(job_dir, ignore_errors=True)
    log.info(f"Cleaned: {job_dir}")
