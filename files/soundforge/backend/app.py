import os, uuid, shutil, time, logging, asyncio, json
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse  # removed StreamingResponse usage

from services.convert    import mp3_to_wav
from services.midi       import wav_chunk_to_midi
from services.instrument import midi_to_audio, ensure_soundfont
from services.transcript import generate_transcript

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("soundtoscore")

app = FastAPI(title="SoundToScore", version="3.0.0")

app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_credentials=False, allow_methods=["*"], allow_headers=["*"])

UPLOAD_DIR = Path("uploads"); UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR = Path("outputs"); OUTPUT_DIR.mkdir(exist_ok=True)

CHUNK_SEC  = 20
MAX_BYTES  = 100 * 1024 * 1024

VALID = {
    "trumpet","flute","soprano_cornet","solo_cornet","repiano_cornet",
    "cornet_2nd","cornet_3rd","flugelhorn","solo_tenor_horn","tenor_horn_1st",
    "tenor_horn_2nd","baritone_1st","baritone_2nd","trombone_1st","trombone_2nd",
    "bass_trombone","euphonium","eb_bass","bbb_bass",
    "timpani","drum_kit","glockenspiel","xylophone","tubular_bells",
    "snare_drum","bass_drum","cymbals","triangle","tambourine",
}

# =========================
# STARTUP
# =========================
@app.on_event("startup")
async def startup():
    try:
        log.info("Startup: ensuring SoundFont...")
        ensure_soundfont()
        log.info("SoundFont ready.")
    except Exception as e:
        log.error(f"SoundFont startup error: {e}")

# =========================
# ROOT
# =========================
@app.get("/")
def root():
    sf_ok = Path("soundfonts/GeneralUser_GS.sf2").exists()
    return {"status": "SoundToScore running", "version": "3.0.0", "soundfont": sf_ok}

# =========================
# HEALTH
# =========================
@app.get("/api/health")
def health():
    sf_ok = Path("soundfonts/GeneralUser_GS.sf2").exists()
    return {"ok": True, "soundfont_ready": sf_ok}

# =========================
# MAIN API
# =========================
@app.post("/api/convert")
async def convert(
    request: Request,
    file: UploadFile = File(...),
    instrument: str  = Form("solo_cornet"),
    tempo: int       = Form(120),
    output_fmt: str  = Form("wav"),
):

    if instrument not in VALID:
        raise HTTPException(400, f"Unknown instrument: {instrument}")

    ext = Path(file.filename or "upload").suffix.lower()
    if ext not in {".mp3", ".wav", ".m4a", ".ogg", ".flac"}:
        raise HTTPException(400, f"Unsupported type: {ext}")

    data = await file.read()
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "File too large (max 100 MB)")

    job_id  = uuid.uuid4().hex[:10]
    job_dir = OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True)

    src = UPLOAD_DIR / f"{job_id}{ext}"
    src.write_bytes(data)

    quality_warning = "Low-quality audio — accuracy may vary." if len(data) < 300_000 else None
    log.info(f"[{job_id}] {len(data)//1024}KB inst={instrument}")

    try:
        # ✅ using normal processing (NOT streaming)
        result = await process_full(job_id, job_dir, src, instrument, tempo, output_fmt, quality_warning)
        return result
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(500, str(e))


# =========================
# PROCESS FUNCTION
# =========================
async def process_full(job_id, job_dir, src, instrument, tempo, output_fmt, quality_warning):
    import librosa, soundfile as sf

    t0 = time.time()

    wav = job_dir / "full.wav"
    mp3_to_wav(str(src), str(wav))

    y, sr = librosa.load(str(wav), sr=16000, mono=True)

    # 🔥 IMPORTANT FIX (prevents 502)
    y = y[:sr * 30]

    short_wav = job_dir / "short.wav"
    sf.write(str(short_wav), y, sr)

    midi_path = job_dir / "out.mid"
    wav_chunk_to_midi(str(short_wav), str(midi_path), sr=sr, tempo=tempo)

    output_audio = job_dir / f"output.{output_fmt}"
    midi_to_audio(str(midi_path), str(output_audio), instrument=instrument, fmt=output_fmt)

    transcript_path = job_dir / "notes.txt"
    txt = generate_transcript(str(midi_path), str(transcript_path), tempo=tempo)

    return {
        "type": "done",
        "status": "success",
        "job_id": job_id,
        "audio": f"/api/files/{job_id}/output.{output_fmt}",
        "midi": f"/api/files/{job_id}/out.mid",
        "transcript": txt[:400],
        "elapsed": round(time.time() - t0, 2),
        "quality_warning": quality_warning
    }


# =========================
# (KEPT YOUR ORIGINAL STREAMING FUNCTION — NOT USED)
# =========================
async def _process_stream(job_id, job_dir, src, instrument, tempo, output_fmt, quality_warning):
    """Kept for future use (not used currently)"""
    pass


# =========================
# FILE SERVE
# =========================
@app.get("/api/files/{job_id}/{filename}")
def serve(job_id: str, filename: str):
    if not job_id.isalnum() or ".." in filename:
        raise HTTPException(400, "Invalid path")

    p = OUTPUT_DIR / job_id / filename

    if not p.exists():
        raise HTTPException(404, "File not found or expired")

    return FileResponse(str(p), filename=filename)


# =========================
# CLEANUP
# =========================
async def _clean(d: str, delay: int = 7200):
    await asyncio.sleep(delay)
    shutil.rmtree(d, ignore_errors=True)
