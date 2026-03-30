import os, uuid, shutil, time, logging, asyncio
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from services.convert    import mp3_to_wav
from services.midi       import wav_chunk_to_midi
from services.instrument import midi_to_audio, ensure_soundfont
from services.transcript import generate_transcript

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("soundtoscore")

app = FastAPI(title="SoundToScore", version="2.0.0")
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

@app.on_event("startup")
async def startup():
    try:
        log.info("Startup: checking SoundFont...")
        ensure_soundfont()
        log.info("SoundFont ready.")
    except Exception as e:
        log.error(f"SoundFont startup error: {e}")

@app.get("/")
def root():
    sf_ok = Path("soundfonts/GeneralUser_GS.sf2").exists()
    return {"status": "SoundToScore running", "version": "2.0.0", "soundfont": sf_ok}

@app.get("/api/health")
def health():
    sf_ok = Path("soundfonts/GeneralUser_GS.sf2").exists()
    return {"ok": True, "soundfont_ready": sf_ok}

@app.post("/api/convert")
async def convert(
    request: Request, background: BackgroundTasks,
    file: UploadFile = File(...),
    instrument: str  = Form("solo_cornet"),
    tempo: int       = Form(120),
    output_fmt: str  = Form("wav"),
):
    if instrument not in VALID:
        raise HTTPException(400, f"Unknown instrument: {instrument}")
    ext = Path(file.filename or "x").suffix.lower()
    if ext not in {".mp3",".wav",".m4a",".ogg",".flac"}:
        raise HTTPException(400, f"Unsupported: {ext}")

    data = await file.read()
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "File too large (max 100MB)")

    job_id  = uuid.uuid4().hex[:10]
    job_dir = OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True)
    src = UPLOAD_DIR / f"{job_id}{ext}"
    src.write_bytes(data)

    warn = "Low-quality audio detected — accuracy may be reduced." if len(data) < 300_000 else None
    log.info(f"[{job_id}] {len(data)/1024:.0f}KB inst={instrument}")
    t0 = time.time()

    try:
        import librosa, soundfile as sf, numpy as np

        wav = job_dir / "full.wav"
        mp3_to_wav(str(src), str(wav))

        y, sr = librosa.load(str(wav), sr=16000, mono=True)
        dur   = len(y) / sr
        csamp = int(CHUNK_SEC * sr)
        n_chunks = max(1, int(np.ceil(len(y) / csamp)))
        log.info(f"[{job_id}] {dur:.1f}s => {n_chunks} sections")

        results = []
        for ci in range(n_chunks):
            cy  = y[ci*csamp : min((ci+1)*csamp, len(y))]
            cst = ci * CHUNK_SEC
            cen = min(cst + CHUNK_SEC, dur)
            cd  = job_dir / f"s{ci+1}"; cd.mkdir()

            cw = cd / "audio.wav"; sf.write(str(cw), cy, sr)
            cm = cd / "out.mid";   wav_chunk_to_midi(str(cw), str(cm), sr=sr, tempo=tempo)
            rn = f"out_{instrument}.{output_fmt}"
            rp = cd / rn;          midi_to_audio(str(cm), str(rp), instrument=instrument, fmt=output_fmt)
            tp = cd / "notes.txt"; txt = generate_transcript(str(cm), str(tp), tempo=tempo)
            del cy

            base = f"/api/files/{job_id}/s{ci+1}"
            results.append({
                "chunk": ci+1,
                "start": round(cst,1), "end": round(cen,1),
                "audio":      f"{base}/{rn}",
                "midi":       f"{base}/out.mid",
                "transcript": f"{base}/notes.txt",
                "transcript_preview": (txt or "")[:400],
            })
            log.info(f"[{job_id}] section {ci+1}/{n_chunks} done")
        del y

    except Exception as exc:
        log.error(f"[{job_id}] {exc}")
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(500, str(exc))
    finally:
        src.unlink(missing_ok=True)

    background.add_task(_clean, str(job_dir))
    return JSONResponse({
        "job_id": job_id, "status": "success",
        "elapsed": round(time.time()-t0,2),
        "total_chunks": n_chunks, "duration": round(dur,1),
        "quality_warning": warn, "chunks": results,
    })

@app.get("/api/files/{job_id}/{sec}/{filename}")
def serve(job_id: str, sec: str, filename: str):
    if not job_id.isalnum() or ".." in sec or ".." in filename:
        raise HTTPException(400, "Invalid path")
    p = OUTPUT_DIR / job_id / sec / filename
    if not p.exists():
        raise HTTPException(404, "File expired or not found")
    return FileResponse(str(p), filename=filename)

async def _clean(d: str, delay: int = 7200):
    await asyncio.sleep(delay)
    shutil.rmtree(d, ignore_errors=True)
