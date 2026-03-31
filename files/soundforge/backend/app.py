"""
SoundToScore — FastAPI Backend v4.0
Architecture:
  POST /api/convert  → saves file, starts background job, returns {job_id} immediately
  GET  /api/status/{job_id} → returns status + completed chunks (poll every 5s)
  GET  /api/files/{job_id}/{section}/{filename} → serves files
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

UPLOAD_DIR = Path("uploads");  UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR = Path("outputs");  OUTPUT_DIR.mkdir(exist_ok=True)

CHUNK_SEC           = 20
MAX_BYTES           = 100 * 1024 * 1024
JOB_TTL             = 7200
MAX_CONCURRENT_JOBS = 2   # ✅ reduced from 3 — prevents CPU overload on Render free tier

VALID_INSTRUMENTS = {
    "trumpet","flute","soprano_cornet","solo_cornet","repiano_cornet",
    "cornet_2nd","cornet_3rd","flugelhorn","solo_tenor_horn","tenor_horn_1st",
    "tenor_horn_2nd","baritone_1st","baritone_2nd","trombone_1st","trombone_2nd",
    "bass_trombone","euphonium","eb_bass","bbb_bass",
    "timpani","drum_kit","glockenspiel","xylophone","tubular_bells",
    "snare_drum","bass_drum","cymbals","triangle","tambourine",
}

# ══════════════════════════════════════════════════════════════
# JOB STATE — persisted to disk as job_dir/meta.json
# ══════════════════════════════════════════════════════════════

def _meta_path(job_id: str) -> Path:
    return OUTPUT_DIR / job_id / "meta.json"

def _read_meta(job_id: str) -> dict:
    p = _meta_path(job_id)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return {}
    return {}

def _write_meta(job_id: str, meta: dict):
    path = _meta_path(job_id)
    tmp  = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(meta, f, indent=2)
    tmp.replace(path)  # atomic write — prevents corruption

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

    # ✅ Mark any interrupted jobs as failed (handles Render restarts)
    if OUTPUT_DIR.exists():
        for job_dir in OUTPUT_DIR.iterdir():
            meta_file = job_dir / "meta.json"
            if meta_file.exists():
                try:
                    meta = json.loads(meta_file.read_text())
                    if meta.get("status") == "processing":
                        log.warning(f"[{job_dir.name}] Interrupted job found — marking failed")
                        _update_meta(job_dir.name,
                                     status="failed",
                                     error="Server restarted. Please re-upload your file.")
                except Exception as e:
                    log.error(f"Error reading meta for {job_dir.name}: {e}")

    # Pre-load SoundFont at startup so first request is fast
    try:
        ensure_soundfont()
        log.info("SoundFont ready.")
    except Exception as e:
        log.error(f"SoundFont startup error: {e}")

# ══════════════════════════════════════════════════════════════
# HEALTH
# ══════════════════════════════════════════════════════════════

@app.get("/")
def root():
    sf_ok = Path("soundfonts/GeneralUser_GS.sf2").exists()
    return {"status": "SoundToScore v4.0 running", "soundfont": sf_ok}

@app.get("/api/health")
def health():
    sf_ok = Path("soundfonts/GeneralUser_GS.sf2").exists()
    return {"ok": True, "soundfont_ready": sf_ok}

# ══════════════════════════════════════════════════════════════
# UPLOAD — returns job_id immediately, starts background task
# ══════════════════════════════════════════════════════════════

@app.post("/api/convert")
async def convert(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    instrument: str  = Form("solo_cornet"),
    tempo: int       = Form(120),
    output_fmt: str  = Form("wav"),
):
    # Validate instrument
    if instrument not in VALID_INSTRUMENTS:
        raise HTTPException(400, f"Unknown instrument: {instrument}")

    # Validate file extension
    ext = Path(file.filename or "upload").suffix.lower()
    if ext not in {".mp3", ".wav", ".m4a", ".ogg", ".flac"}:
        raise HTTPException(400, f"Unsupported file type: {ext}")

    # Read and size-check
    data = await file.read()
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "File too large (max 100 MB)")

    # ✅ Check concurrent job limit before creating new job
    active_jobs = 0
    if OUTPUT_DIR.exists():
        for job_dir in OUTPUT_DIR.iterdir():
            meta_file = job_dir / "meta.json"
            if meta_file.exists():
                try:
                    m = json.loads(meta_file.read_text())
                    if m.get("status") == "processing":
                        active_jobs += 1
                except Exception:
                    pass

    if active_jobs >= MAX_CONCURRENT_JOBS:
        raise HTTPException(503, "Server busy — please try again in a moment.")

    # Create job
    job_id  = uuid.uuid4().hex[:10]
    job_dir = OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True)

    src = UPLOAD_DIR / f"{job_id}{ext}"
    src.write_bytes(data)

    quality_warning = ("Low-quality audio detected — accuracy may be reduced."
                       if len(data) < 300_000 else None)

    # ✅ Write initial meta immediately (checkpoint)
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

    log.info(f"[{job_id}] Created — {len(data)//1024}KB inst={instrument}")

    # ✅ Start background processing — does NOT block the HTTP response
    background.add_task(_run_in_thread, job_id, str(src), instrument, tempo, output_fmt)

    # ✅ Return job_id immediately
    return JSONResponse({"job_id": job_id, "status": "processing"})


def _run_in_thread(job_id, src_path, instrument, tempo, output_fmt):
    """Spawn a daemon thread for heavy processing — keeps event loop free."""
    t = threading.Thread(
        target=_process_job,
        args=(job_id, src_path, instrument, tempo, output_fmt),
        daemon=True
    )
    t.start()


# ══════════════════════════════════════════════════════════════
# BACKGROUND PROCESSING — chunk-by-chunk with checkpointing
# ══════════════════════════════════════════════════════════════

def _process_job(job_id: str, src_path: str, instrument: str, tempo: int, output_fmt: str):
    import librosa, soundfile as sf, numpy as np

    job_dir = OUTPUT_DIR / job_id
    t0 = time.time()
    log.info(f"[{job_id}] Thread started")

    try:
        # Step 1: MP3/M4A → WAV
        wav = job_dir / "full.wav"
        mp3_to_wav(src_path, str(wav))
        Path(src_path).unlink(missing_ok=True)

        # Step 2: Load audio
        y, sr = librosa.load(str(wav), sr=16000, mono=True)
        dur   = len(y) / sr
        csamp = int(CHUNK_SEC * sr)
        n_ch  = max(1, int(np.ceil(len(y) / csamp)))

        log.info(f"[{job_id}] {dur:.1f}s => {n_ch} chunks")
        _update_meta(job_id, total_chunks=n_ch, duration=round(dur, 1))

        # Step 3: Check which chunks already done (resume after restart)
        meta = _read_meta(job_id)
        done_so_far = {c["chunk"] for c in meta.get("chunks", [])}

        # Step 4: Process each chunk
        for ci in range(n_ch):
            chunk_num = ci + 1

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

            # ✅ Save chunk to meta.json immediately (progressive results)
            base = f"/api/files/{job_id}/s{chunk_num}"
            chunk_info = {
                "chunk":               chunk_num,
                "start":               cst,
                "end":                 cen,
                "audio":               f"{base}/{rn}",
                "midi":                f"{base}/out.mid",
                "transcript":          f"{base}/notes.txt",
                "transcript_preview":  (txt or "")[:400],
            }

            meta = _read_meta(job_id)
            chunks = meta.get("chunks", [])
            chunks.append(chunk_info)
            meta.update({
                "chunks":      chunks,
                "done_chunks": len(chunks),
                "elapsed":     round(time.time() - t0, 2),
            })
            _write_meta(job_id, meta)

            log.info(f"[{job_id}] Chunk {chunk_num}/{n_ch} done")

        del y  # free full audio from RAM

        # Step 5: Mark complete
        _update_meta(job_id, status="success", elapsed=round(time.time() - t0, 2))
        log.info(f"[{job_id}] All done in {time.time()-t0:.1f}s")

        # Schedule cleanup after 2 hours
        def _cleanup():
            time.sleep(JOB_TTL)
            shutil.rmtree(str(job_dir), ignore_errors=True)
            log.info(f"[{job_id}] Cleaned up")
        threading.Thread(target=_cleanup, daemon=True).start()

    except Exception as exc:
        import traceback
        log.error(f"[{job_id}] FAILED: {exc}\n{traceback.format_exc()}")
        _update_meta(job_id, status="failed", error=str(exc))
        Path(src_path).unlink(missing_ok=True)


# ══════════════════════════════════════════════════════════════
# STATUS — frontend polls this every 5 seconds
# ══════════════════════════════════════════════════════════════

@app.get("/api/status/{job_id}")
def status(job_id: str):
    if not job_id.isalnum():
        raise HTTPException(400, "Invalid job ID")

    meta = _read_meta(job_id)
    if not meta:
        raise HTTPException(404, "Job not found — may have expired")

    return JSONResponse({
        "job_id":          meta.get("job_id"),
        "status":          meta.get("status"),      # processing | success | failed
        "total_chunks":    meta.get("total_chunks", 0),
        "done_chunks":     meta.get("done_chunks",  0),
        "duration":        meta.get("duration",     0),
        "elapsed":         meta.get("elapsed",      0),
        "quality_warning": meta.get("quality_warning"),
        "error":           meta.get("error"),
        "chunks":          meta.get("chunks",       []),
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
