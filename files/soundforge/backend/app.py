"""
SoundForge AI — FastAPI Backend
================================
Run locally:  uvicorn app:app --reload --port 8000
"""

import os, uuid, shutil, time, logging, asyncio
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from services.convert    import mp3_to_wav
from services.midi       import wav_to_midi
from services.instrument import midi_to_audio
from services.transcript import generate_transcript

# ── Logging ───────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger("soundforge")

# ── Rate limiter ──────────────────────────────────────────────
limiter = Limiter(key_func=get_remote_address)

# ── App ───────────────────────────────────────────────────────
app = FastAPI(
    title="SoundForge AI",
    version="1.0.0",
    description="MP3 to MIDI converter with brass band instrument rendering",
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── CORS ──────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Directories ───────────────────────────────────────────────
UPLOAD_DIR = Path("uploads")
OUTPUT_DIR = Path("outputs")
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

# ── Limits ────────────────────────────────────────────────────
MAX_BYTES = 50 * 1024 * 1024   # 50 MB
ALLOWED_EXTENSIONS = {".mp3", ".wav", ".m4a", ".ogg", ".flac"}

# ── Valid instrument IDs ──────────────────────────────────────
VALID_INSTRUMENTS = {
    "trumpet","flute","soprano_cornet","solo_cornet","repiano_cornet",
    "cornet_2nd","cornet_3rd","flugelhorn","solo_tenor_horn","tenor_horn_1st",
    "tenor_horn_2nd","baritone_1st","baritone_2nd","trombone_1st","trombone_2nd",
    "bass_trombone","euphonium","eb_bass","bbb_bass",
    "timpani","drum_kit","glockenspiel","xylophone","tubular_bells",
    "snare_drum","bass_drum","cymbals","triangle","tambourine",
}

# ═══════════════════════════════════════════════════
# HEALTH CHECK
# ═══════════════════════════════════════════════════
@app.get("/")
def health():
    return {"status": "SoundForge AI is running 🎵", "version": "1.0.0"}

@app.get("/api/health")
def api_health():
    return {"ok": True}


# ═══════════════════════════════════════════════════
# CONVERT
# ═══════════════════════════════════════════════════
@app.post("/api/convert")
@limiter.limit("10/minute")
async def convert(
    request:    Request,
    background: BackgroundTasks,
    file:       UploadFile = File(...),
    instrument: str        = Form("solo_cornet"),
    quality:    str        = Form("high"),
    tempo:      int        = Form(120),
    output_fmt: str        = Form("wav"),
):
    # Validate instrument
    if instrument not in VALID_INSTRUMENTS:
        raise HTTPException(400, f"Unknown instrument: {instrument}")

    # Validate file extension
    original_name = file.filename or "upload"
    ext = Path(original_name).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Unsupported file type: {ext}. Use MP3, WAV or M4A.")

    # Read and size-check
    data = await file.read()
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "File too large. Maximum size is 50 MB.")

    # Create job directory
    job_id  = uuid.uuid4().hex[:10]
    job_dir = OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    src_path = UPLOAD_DIR / f"{job_id}_input{ext}"
    src_path.write_bytes(data)
    log.info(f"[{job_id}] Saved {len(data)/1024:.1f} KB  instrument={instrument}")

    t0 = time.time()

    try:
        # Step 1 — MP3 → WAV
        wav_path = job_dir / "audio.wav"
        mp3_to_wav(str(src_path), str(wav_path))
        log.info(f"[{job_id}] WAV ready  {time.time()-t0:.1f}s")

        # Step 2 — WAV → MIDI
        midi_path = job_dir / "output.mid"
        wav_to_midi(str(wav_path), str(midi_path))
        log.info(f"[{job_id}] MIDI ready  {time.time()-t0:.1f}s")

        # Step 3 — MIDI → instrument audio
        rendered_name = f"output_{instrument}.{output_fmt}"
        rendered_path = job_dir / rendered_name
        midi_to_audio(str(midi_path), str(rendered_path), instrument=instrument, fmt=output_fmt)
        log.info(f"[{job_id}] Audio rendered  {time.time()-t0:.1f}s")

        # Step 4 — transcript
        transcript_path = job_dir / "notes.txt"
        generate_transcript(str(midi_path), str(transcript_path), tempo=tempo)
        log.info(f"[{job_id}] Transcript done  {time.time()-t0:.1f}s")

    except Exception as exc:
        log.error(f"[{job_id}] Pipeline failed: {exc}")
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(500, f"Processing error: {exc}")

    finally:
        src_path.unlink(missing_ok=True)

    # Delete files after 1 hour
    background.add_task(_cleanup, str(job_dir), delay=3600)

    elapsed = round(time.time() - t0, 2)
    base    = f"/api/download/{job_id}"

    return JSONResponse({
        "job_id":   job_id,
        "status":   "success",
        "elapsed":  elapsed,
        "downloads": {
            "midi":       f"{base}/output.mid",
            "audio":      f"{base}/{rendered_name}",
            "transcript": f"{base}/notes.txt",
        }
    })


# ═══════════════════════════════════════════════════
# DOWNLOAD
# ═══════════════════════════════════════════════════
@app.get("/api/download/{job_id}/{filename}")
def download(job_id: str, filename: str):
    if not job_id.isalnum():
        raise HTTPException(400, "Invalid job ID")
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(400, "Invalid filename")

    path = OUTPUT_DIR / job_id / filename
    if not path.exists():
        raise HTTPException(404, "File not found or expired (files are deleted after 1 hour)")

    return FileResponse(
        str(path),
        filename=filename,
        headers={"Access-Control-Allow-Origin": "*"}
    )


# ── Cleanup ───────────────────────────────────────────────────
async def _cleanup(job_dir: str, delay: int = 3600):
    await asyncio.sleep(delay)
    shutil.rmtree(job_dir, ignore_errors=True)
    log.info(f"Cleaned up: {job_dir}")
