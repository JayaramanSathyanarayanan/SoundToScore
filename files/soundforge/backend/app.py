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

from services.convert import mp3_to_wav
from services.midi import wav_to_midi
from services.instrument import midi_to_audio
from services.transcript import generate_transcript

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("soundforge")

app = FastAPI(title="SoundForge AI", version="1.0.0")

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

MAX_BYTES = 50 * 1024 * 1024

VALID_INSTRUMENTS = {
    "trumpet", "flute", "soprano_cornet", "solo_cornet", "repiano_cornet",
    "cornet_2nd", "cornet_3rd", "flugelhorn", "solo_tenor_horn", "tenor_horn_1st",
    "tenor_horn_2nd", "baritone_1st", "baritone_2nd", "trombone_1st", "trombone_2nd",
    "bass_trombone", "euphonium", "eb_bass", "bbb_bass",
    "timpani", "drum_kit", "glockenspiel", "xylophone", "tubular_bells",
    "snare_drum", "bass_drum", "cymbals", "triangle", "tambourine",
}


@app.get("/")
def health():
    return {"status": "SoundForge AI is running", "version": "1.0.0"}


@app.get("/api/health")
def api_health():
    return {"ok": True}


@app.post("/api/convert")
async def convert(
    request: Request,
    background: BackgroundTasks,
    file: UploadFile = File(...),
    instrument: str = Form("solo_cornet"),
    quality: str = Form("high"),
    tempo: int = Form(120),
    output_fmt: str = Form("wav"),
):
    if instrument not in VALID_INSTRUMENTS:
        raise HTTPException(400, f"Unknown instrument: {instrument}")

    original_name = file.filename or "upload"
    ext = Path(original_name).suffix.lower()
    allowed = {".mp3", ".wav", ".m4a", ".ogg", ".flac"}
    if ext not in allowed:
        raise HTTPException(400, f"Unsupported file type: {ext}")

    data = await file.read()
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "File too large. Max 50MB.")

    job_id = uuid.uuid4().hex[:10]
    job_dir = OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    src_path = UPLOAD_DIR / f"{job_id}_input{ext}"
    src_path.write_bytes(data)
    log.info(f"[{job_id}] Saved {len(data)/1024:.1f} KB instrument={instrument}")

    t0 = time.time()

    try:
        wav_path = job_dir / "audio.wav"
        mp3_to_wav(str(src_path), str(wav_path))
        log.info(f"[{job_id}] WAV done {time.time()-t0:.1f}s")

        midi_path = job_dir / "output.mid"
        wav_to_midi(str(wav_path), str(midi_path))
        log.info(f"[{job_id}] MIDI done {time.time()-t0:.1f}s")

        rendered_name = f"output_{instrument}.{output_fmt}"
        rendered_path = job_dir / rendered_name
        midi_to_audio(str(midi_path), str(rendered_path), instrument=instrument, fmt=output_fmt)
        log.info(f"[{job_id}] Audio done {time.time()-t0:.1f}s")

        transcript_path = job_dir / "notes.txt"
        generate_transcript(str(midi_path), str(transcript_path), tempo=tempo)
        log.info(f"[{job_id}] Transcript done {time.time()-t0:.1f}s")

    except Exception as exc:
        log.error(f"[{job_id}] Failed: {exc}")
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(500, f"Processing error: {exc}")

    finally:
        src_path.unlink(missing_ok=True)

    background.add_task(_cleanup, str(job_dir))

    base = f"/api/download/{job_id}"
    return JSONResponse({
        "job_id": job_id,
        "status": "success",
        "elapsed": round(time.time() - t0, 2),
        "downloads": {
            "midi": f"{base}/output.mid",
            "audio": f"{base}/{rendered_name}",
            "transcript": f"{base}/notes.txt",
        }
    })


@app.get("/api/download/{job_id}/{filename}")
def download(job_id: str, filename: str):
    if not job_id.isalnum():
        raise HTTPException(400, "Invalid job ID")
    if ".." in filename or "/" in filename:
        raise HTTPException(400, "Invalid filename")

    path = OUTPUT_DIR / job_id / filename
    if not path.exists():
        raise HTTPException(404, "File not found or expired")

    return FileResponse(str(path), filename=filename)


async def _cleanup(job_dir: str, delay: int = 3600):
    await asyncio.sleep(delay)
    shutil.rmtree(job_dir, ignore_errors=True)
    log.info(f"Cleaned: {job_dir}")
