"""
SoundToScore — FastAPI Backend v4.0
====================================
Architecture:
  POST /api/convert  → saves file, starts background job, returns {job_id} immediately
  GET  /api/status/{job_id} → returns status + completed chunks (poll every 3s)
  GET  /api/files/{job_id}/{path} → serves audio/midi/transcript files

Features:
  - Background processing (never blocks the HTTP request)
  - Chunk-based processing (20s per chunk, one at a time — saves RAM)
  - Checkpointing (job survives server restart via JSON on disk)
  - Progressive results (frontend gets each chunk as it completes)
  - localStorage resume (frontend resumes on page refresh)
  - Graceful error handling
"""

import os, uuid, shutil, time, logging, asyncio, json, threading
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from services.convert    import mp3_to_wav
from services.midi       import wav_chunk_to_midi
from services.instrument import midi_to_audio, ensure_soundfont
from services.transcript import generate_transcript

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("soundtoscore")

app = FastAPI(title="SoundToScore", version="4.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Directories ────────────────────────────────────────────────
UPLOAD_DIR = Path("uploads");  UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR = Path("outputs");  OUTPUT_DIR.mkdir(exist_ok=True)

# ── Constants ──────────────────────────────────────────────────
CHUNK_SEC = 20          # seconds per chunk
MAX_BYTES = 100 * 1024 * 1024  # 100 MB upload limit
JOB_TTL   = 7200        # seconds before auto-cleanup (2 hours)

MAX_CONCURRENT_JOBS = 3

VALID_INSTRUMENTS = {
    "trumpet","flute","soprano_cornet","solo_cornet","repiano_cornet",
    "cornet_2nd","cornet_3rd","flugelhorn","solo_tenor_horn","tenor_horn_1st",
    "tenor_horn_2nd","baritone_1st","baritone_2nd","trombone_1st","trombone_2nd",
    "bass_trombone","euphonium","eb_bass","bbb_bass",
    "timpani","drum_kit","glockenspiel","xylophone","tubular_bells",
    "snare_drum","bass_drum","cymbals","triangle","tambourine",
}

# ══════════════════════════════════════════════════════════════
# JOB STATE  (persisted to disk as job_dir/meta.json)
# ══════════════════════════════════════════════════════════════

def _meta_path(job_id: str) -> Path:
    return OUTPUT_DIR / job_id / "meta.json"

def _read_meta(job_id: str) -> dict:
    p = _meta_path(job_id)
    if p.exists():
        return json.loads(p.read_text())
    return {}

def _write_meta(job_id: str, meta: dict):
    path = _meta_path(job_id)
    tmp  = path.with_suffix(".tmp")

    with open(tmp, "w") as f:
        json.dump(meta, f, indent=2)

    tmp.replace(path)

def _update_meta(job_id: str, **kwargs):
    meta = _read_meta(job_id)
    meta.update(kwargs)
    _write_meta(job_id, meta)

# ══════════════════════════════════════════════════════════════
# STARTUP
# ══════════════════════════════════════════════════════════════

@app.on_event("startup")
async def startup():
    log.info("SoundToScore v4.0 starting...")
    # Resume any jobs that were processing when server restarted
    for job_dir in OUTPUT_DIR.iterdir():
        meta_file = job_dir / "meta.json"
        if meta_file.exists():
            meta = json.loads(meta_file.read_text())
            if meta.get("status") == "processing":
                log.warning(f"[{job_dir.name}] Found interrupted job — marking failed")
                _update_meta(job_dir.name, status="failed",
                             error="Server restarted during processing. Please re-upload.")
    # Pre-load SoundFont
    try:
        ensure_soundfont()
        log.info("SoundFont ready.")
    except Exception as e:
        log.error(f"SoundFont error: {e}")

# ══════════════════════════════════════════════════════════════
# HEALTH
# ══════════════════════════════════════════════════════════════

@app.get("/")
def root():
    return {"status": "SoundToScore v4.0 running",
            "soundfont": Path("soundfonts/GeneralUser_GS.sf2").exists()}

@app.get("/api/health")
def health():
    return {"ok": True,
            "soundfont_ready": Path("soundfonts/GeneralUser_GS.sf2").exists()}

# ══════════════════════════════════════════════════════════════
# UPLOAD → returns job_id immediately, starts background task
# ══════════════════════════════════════════════════════════════

@app.post("/api/convert")
async def convert(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    instrument: str  = Form("solo_cornet"),
    tempo: int       = Form(120),
    output_fmt: str  = Form("wav"),
):
    # Validate
    if instrument not in VALID_INSTRUMENTS:
        raise HTTPException(400, f"Unknown instrument: {instrument}")
    ext = Path(file.filename or "upload").suffix.lower()
    if ext not in {".mp3", ".wav", ".m4a", ".ogg", ".flac"}:
        raise HTTPException(400, f"Unsupported file type: {ext}")

    # Read & size-check
    data = await file.read()
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "File too large (max 100 MB)")

    # ── LIMIT CONCURRENT USERS ─────────────────────
    active_jobs = 0
    
    for job_dir in OUTPUT_DIR.iterdir():
        meta_file = job_dir / "meta.json"
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text())
                if meta.get("status") == "processing":
                    active_jobs += 1
            except:
                pass
    
    if active_jobs >= MAX_CONCURRENT_JOBS:
        raise HTTPException(503, "Server busy, please try again in a moment")
    
    # ✅ CREATE JOB ONLY AFTER CHECK
    job_id  = uuid.uuid4().hex[:10]
      
    job_dir = OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True)

    src = UPLOAD_DIR / f"{job_id}{ext}"
    src.write_bytes(data)

    quality_warning = ("Low-quality audio detected — accuracy may be reduced."
                       if len(data) < 300_000 else None)

    # Write initial metadata to disk (checkpoint)
    _write_meta(job_id, {
        "job_id":          job_id,
        "status":          "processing",
        "instrument":      instrument,
        "tempo":           tempo,
        "output_fmt":      output_fmt,
        "quality_warning": quality_warning,
        "total_chunks":    0,
        "done_chunks":     0,
        "chunks":          [],
        "error":           None,
        "elapsed":         0,
        "duration":        0,
        "created_at":      time.time(),
    })

    log.info(f"[{job_id}] Job created — {len(data)//1024}KB inst={instrument}")

    # Start background processing — DOES NOT BLOCK the response
    background.add_task(
        _run_in_thread,
        job_id, str(src), instrument, tempo, output_fmt
    )

    # Return job_id immediately ✅
    return JSONResponse({"job_id": job_id, "status": "processing"})


def _run_in_thread(job_id, src_path, instrument, tempo, output_fmt):
    """Run heavy processing in a thread pool so it doesn't block the event loop."""
    import threading
    t = threading.Thread(
        target=_process_job,
        args=(job_id, src_path, instrument, tempo, output_fmt),
        daemon=True
    )
    t.start()


# ══════════════════════════════════════════════════════════════
# BACKGROUND PROCESSING  (runs in thread, chunks with checkpoint)
# ══════════════════════════════════════════════════════════════

def _process_job(job_id: str, src_path: str, instrument: str, tempo: int, output_fmt: str):
    import librosa, soundfile as sf, numpy as np

    job_dir = OUTPUT_DIR / job_id
    t0 = time.time()
    log.info(f"[{job_id}] Processing started in thread")

    try:
        # ── Step 1: convert to WAV ────────────────────────────
        wav = job_dir / "full.wav"
        mp3_to_wav(src_path, str(wav))
        Path(src_path).unlink(missing_ok=True)

        # ── Step 2: load audio ────────────────────────────────
        y, sr = librosa.load(str(wav), sr=16000, mono=True)
        dur   = len(y) / sr
        csamp = int(CHUNK_SEC * sr)
        n_ch  = max(1, int(np.ceil(len(y) / csamp)))

        log.info(f"[{job_id}] {dur:.1f}s => {n_ch} chunks")
        _update_meta(job_id, total_chunks=n_ch, duration=round(dur, 1))

        # ── Step 3: process each chunk ────────────────────────
        # Check existing chunks for resume after restart
        meta = _read_meta(job_id)
        done_so_far = {c["chunk"] for c in meta.get("chunks", [])}

        for ci in range(n_ch):
            chunk_num = ci + 1

            # Skip already-processed chunks (checkpoint resume)
            if chunk_num in done_so_far:
                log.info(f"[{job_id}] Chunk {chunk_num} already done — skipping")
                continue

            cy  = y[ci * csamp : min((ci + 1) * csamp, len(y))]
            cst = round(ci * CHUNK_SEC, 1)
            cen = round(min(cst + CHUNK_SEC, dur), 1)
            cd  = job_dir / f"s{chunk_num}"
            cd.mkdir(exist_ok=True)

            # Write chunk WAV
            cw = cd / "audio.wav"
            sf.write(str(cw), cy, sr)
            del cy  # free RAM immediately

            # WAV → MIDI
            cm = cd / "out.mid"
            wav_chunk_to_midi(str(cw), str(cm), sr=sr, tempo=tempo)

            # MIDI → instrument audio
            rn = f"out_{instrument}.{output_fmt}"
            rp = cd / rn
            midi_to_audio(str(cm), str(rp), instrument=instrument, fmt=output_fmt)

            # Transcript
            tp  = cd / "notes.txt"
            txt = generate_transcript(str(cm), str(tp), tempo=tempo)

            # Checkpoint: save chunk result to meta.json immediately ✅
            base  = f"/api/files/{job_id}/s{chunk_num}"
            chunk_info = {
                "chunk":      chunk_num,
                "start":      cst,
                "end":        cen,
                "audio":      f"{base}/{rn}",
                "midi":       f"{base}/out.mid",
                "transcript": f"{base}/notes.txt",
                "transcript_preview": (txt or "")[:400],
            }

            meta = _read_meta(job_id)

            chunks = meta.get("chunks", [])
            chunks.append(chunk_info)
            
            meta.update({
                "chunks": chunks,
                "done_chunks": len(chunks),
                "elapsed": round(time.time() - t0, 2)
            })
            
            _write_meta(job_id, meta)

            log.info(f"[{job_id}] Chunk {chunk_num}/{n_ch} done and saved")

        del y  # free full audio array

        # ── Step 4: mark complete ─────────────────────────────
        _update_meta(job_id,
                     status="success",
                     elapsed=round(time.time() - t0, 2))
        log.info(f"[{job_id}] All {n_ch} chunks done in {time.time()-t0:.1f}s")

        # Schedule cleanup
        def _delayed_cleanup():
            time.sleep(JOB_TTL)

            meta = _read_meta(job_id)
            if meta.get("status") == "success":
                shutil.rmtree(str(job_dir), ignore_errors=True)
                log.info(f"[{job_id}] Cleaned up")

        threading.Thread(target=_delayed_cleanup, daemon=True).start()
    except Exception as exc:
        import traceback
        log.error(f"[{job_id}] FAILED: {exc}\n{traceback.format_exc()}")
        _update_meta(job_id, status="failed", error=str(exc))
        Path(src_path).unlink(missing_ok=True)


# ══════════════════════════════════════════════════════════════
# STATUS ENDPOINT  (frontend polls this every 3 seconds)
# ══════════════════════════════════════════════════════════════

@app.get("/api/status/{job_id}")
def status(job_id: str):
    if not job_id.isalnum():
        raise HTTPException(400, "Invalid job ID")

    meta = _read_meta(job_id)
    if not meta:
        raise HTTPException(404, "Job not found (may have expired)")

    # Return everything the frontend needs
    return JSONResponse({
        "job_id":          meta.get("job_id"),
        "status":          meta.get("status"),          # processing | success | failed
        "total_chunks":    meta.get("total_chunks", 0),
        "done_chunks":     meta.get("done_chunks", 0),
        "duration":        meta.get("duration", 0),
        "elapsed":         meta.get("elapsed", 0),
        "quality_warning": meta.get("quality_warning"),
        "error":           meta.get("error"),
        "chunks":          meta.get("chunks", []),       # list of completed chunks
    })


# ══════════════════════════════════════════════════════════════
# FILE SERVE
# ══════════════════════════════════════════════════════════════

@app.get("/api/files/{job_id}/{section}/{filename}")
def serve_chunk_file(job_id: str, section: str, filename: str):
    if not job_id.isalnum() or ".." in section or ".." in filename:
        raise HTTPException(400, "Invalid path")
    p = OUTPUT_DIR / job_id / section / filename
    if not p.exists():
        raise HTTPException(404, "File not found or expired")
    return FileResponse(str(p), filename=filename)

@app.get("/api/files/{job_id}/{filename}")
def serve_job_file(job_id: str, filename: str):
    if not job_id.isalnum() or ".." in filename:
        raise HTTPException(400, "Invalid path")
    p = OUTPUT_DIR / job_id / filename
    if not p.exists():
        raise HTTPException(404, "File not found or expired")
    return FileResponse(str(p), filename=filename)
