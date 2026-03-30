import os, uuid, shutil, time, logging, asyncio
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from services.convert    import mp3_to_wav
from services.midi       import wav_chunk_to_midi
from services.instrument import midi_to_audio, ensure_soundfont
from services.transcript import generate_transcript

jobs = {}

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("soundtoscore")

app = FastAPI(title="SoundToScore", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_credentials=False, allow_methods=["*"], allow_headers=["*"])

UPLOAD_DIR = Path("uploads"); UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR = Path("outputs"); OUTPUT_DIR.mkdir(exist_ok=True)
CHUNK_SEC  = 10
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
    request: Request,
    file: UploadFile = File(...),
    instrument: str  = Form("solo_cornet"),
    tempo: int       = Form(120),
    output_fmt: str  = Form("wav"),
):
    if instrument not in VALID:
        raise HTTPException(400, f"Unknown instrument: {instrument}")

    ext = Path(file.filename or "x").suffix.lower()
    data = await file.read()

    job_id  = uuid.uuid4().hex[:10]
    job_dir = OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True)

    src = UPLOAD_DIR / f"{job_id}{ext}"
    src.write_bytes(data)

    jobs[job_id] = {
        "status": "processing",
        "chunks": [],
        "total": 0
    }

    # ✅ FIXED (inside function)
    asyncio.create_task(process_job(job_id, src, instrument, tempo, output_fmt))

    return {"job_id": job_id}

async def process_job(job_id, src, instrument, tempo, output_fmt):
    try:
        import librosa, soundfile as sf, numpy as np

        job_dir = OUTPUT_DIR / job_id
        wav = job_dir / "full.wav"

        mp3_to_wav(str(src), str(wav))

        y, sr = librosa.load(str(wav), sr=16000, mono=True)
        csamp = int(CHUNK_SEC * sr)
        n_chunks = max(1, int(np.ceil(len(y) / csamp)))

        jobs[job_id]["total"] = n_chunks

        results = []

        for ci in range(n_chunks):
            log.info(f"Processing chunk {ci+1}/{n_chunks}")
            cy = y[ci*csamp : min((ci+1)*csamp, len(y))]

            cd = job_dir / f"s{ci+1}"
            cd.mkdir(exist_ok=True)

            cw = cd / "audio.wav"
            sf.write(str(cw), cy, sr)

            cm = cd / "out.mid"
            wav_chunk_to_midi(str(cw), str(cm), sr=sr, tempo=tempo)

            rn = f"out_{instrument}.{output_fmt}"
            rp = cd / rn
            midi_to_audio(str(cm), str(rp), instrument=instrument, fmt=output_fmt)

            txt = generate_transcript(str(cm), str(cd/"notes.txt"), tempo=tempo)

            base = f"/api/files/{job_id}/s{ci+1}"

            results.append({
                "chunk": ci+1,
                "audio": f"{base}/{rn}",
                "midi": f"{base}/out.mid",
                "transcript": txt[:200]
            })

            jobs[job_id]["chunks"] = results

        if results:
           jobs[job_id]["status"] = "done"
        else:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = "No chunks generated"

    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)

    finally:
        src.unlink(missing_ok=True)

@app.get("/api/status/{job_id}")
def status(job_id: str):
    return jobs.get(job_id, {"status": "not_found"})
    
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
  

