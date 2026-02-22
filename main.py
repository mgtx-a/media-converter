"""
main.py
-------
FastAPI backend for the Media Converter web app.
"""

import os
import shutil
import subprocess
import threading
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from converter_core import build_cmds, get_duration
from job_store import store as job_store

# ── Constants ──────────────────────────────────────────────────────────────────
UPLOAD_DIR   = Path("/tmp/mc_uploads")
OUTPUT_DIR   = Path("/tmp/mc_outputs")
ASSETS_DIR   = Path(__file__).parent / "assets"
COUNTDOWN_MP4 = ASSETS_DIR / "countdown.mp4"
MAX_UPLOAD_BYTES = 500 * 1024 * 1024   # 500 MB
CHUNK_SIZE       = 1024 * 1024          # 1 MB

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Media Converter")

# Serve static files (index.html)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


# ── Root ───────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def root():
    html_path = Path(__file__).parent / "static" / "index.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


# ── Upload ─────────────────────────────────────────────────────────────────────
@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    # Check extension
    suffix = Path(file.filename or "").suffix.lower()
    allowed = {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".m4v", ".flv", ".ts", ".3gp"}
    if suffix not in allowed:
        raise HTTPException(400, f"Unsupported file type: {suffix}")

    file_id  = str(uuid.uuid4())
    dest_dir = UPLOAD_DIR / file_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest     = dest_dir / (file.filename or f"video{suffix}")

    # Stream to disk with size cap
    written = 0
    try:
        with dest.open("wb") as f:
            while True:
                chunk = await file.read(CHUNK_SIZE)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    dest.unlink(missing_ok=True)
                    raise HTTPException(413, "File exceeds 500 MB limit.")
                f.write(chunk)
    finally:
        await file.close()

    # Probe duration
    duration = get_duration(str(dest))

    return {
        "file_id":  file_id,
        "filename": file.filename,
        "duration": duration,
    }


# ── Convert ────────────────────────────────────────────────────────────────────
@app.post("/convert", status_code=202)
async def convert(
    file_id:    str  = Form(...),
    filename:   str  = Form(...),
    fmt:        str  = Form(...),          # AVI | MOV | MP4
    countdown:  bool = Form(False),
    size_limit: bool = Form(False),
    size_mb:    float = Form(15.0),
    trim_start: float = Form(0.0),
    trim_end:   float = Form(-1.0),        # -1 means no trim
):
    # Locate uploaded file
    upload_dir = UPLOAD_DIR / file_id
    if not upload_dir.exists():
        raise HTTPException(404, "Upload not found. Please re-upload the file.")

    matches = list(upload_dir.iterdir())
    if not matches:
        raise HTTPException(404, "Uploaded file missing on disk.")
    src_path = matches[0]

    # Validate format
    fmt_upper = fmt.upper()
    if fmt_upper not in ("AVI", "MOV", "MP4"):
        raise HTTPException(400, f"Unknown format: {fmt}")

    # Create job
    job = job_store.create()

    # Spawn worker thread (non-blocking)
    t = threading.Thread(
        target=_conversion_worker,
        args=(job.job_id, src_path, fmt_upper, countdown, size_limit, size_mb,
              trim_start, trim_end),
        daemon=True,
    )
    t.start()

    return {"job_id": job.job_id}


def _conversion_worker(job_id: str, src_path: Path, fmt: str,
                        use_countdown: bool, use_size_limit: bool, size_mb: float,
                        trim_start: float, trim_end: float):
    """Runs in a background thread. Updates job_store throughout."""
    job_store.update(job_id, status="running", progress=5, message="Starting…")

    try:
        ext_map = {"AVI": ".avi", "MOV": ".mov", "MP4": ".mp4"}
        ext     = ext_map[fmt]
        out_dir = OUTPUT_DIR / job_id
        out_dir.mkdir(parents=True, exist_ok=True)
        dst      = out_dir / (src_path.stem + ext)
        passlog  = str(out_dir / f"{src_path.stem}_ffpass")

        # Countdown
        countdown_path = None
        if use_countdown:
            if not COUNTDOWN_MP4.exists():
                raise FileNotFoundError("Countdown file not found on server.")
            countdown_path = str(COUNTDOWN_MP4)

        # Size limit
        target_mb = size_mb if use_size_limit else None

        # Trim
        trim = None
        if trim_end > trim_start:
            trim = (trim_start, trim_end)
            input_dur = trim_end - trim_start
        else:
            input_dur = get_duration(str(src_path))

        countdown_dur = get_duration(countdown_path) if countdown_path else 0.0
        total_dur     = input_dur + countdown_dur

        cmds = build_cmds(
            src=str(src_path),
            dst=str(dst),
            fmt_label=fmt,
            countdown_path=countdown_path,
            target_mb=target_mb,
            total_dur=total_dur,
            passlog=passlog,
            trim=trim,
        )

        for i, cmd in enumerate(cmds):
            if len(cmds) > 1 and i == 0:
                job_store.update(job_id, progress=10, message="Pass 1/2…")
            elif len(cmds) > 1:
                job_store.update(job_id, progress=60, message="Pass 2/2…")
            else:
                job_store.update(job_id, progress=15, message="Encoding…")

            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=1800
            )
            if result.returncode != 0:
                err = (result.stderr or "").strip()[-800:] or "FFmpeg error."
                raise RuntimeError(err)

        job_store.update(
            job_id,
            status="done",
            progress=100,
            message="Conversion complete.",
            output_path=str(dst),
        )

    except Exception as exc:
        job_store.update(
            job_id,
            status="error",
            message=str(exc)[:500],
        )
    finally:
        # Clean up pass log files
        for suf in ["-0.log", "-0.log.mbtree"]:
            try:
                Path(passlog + suf).unlink(missing_ok=True)
            except Exception:
                pass


# ── Status ─────────────────────────────────────────────────────────────────────
@app.get("/status/{job_id}")
async def status(job_id: str):
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found.")
    return {
        "status":   job.status,
        "progress": job.progress,
        "message":  job.message,
    }


# ── Download ───────────────────────────────────────────────────────────────────
@app.get("/download/{job_id}")
async def download(job_id: str):
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found.")
    if job.status != "done":
        raise HTTPException(400, "Conversion not yet complete.")
    if not job.output_path or not Path(job.output_path).exists():
        raise HTTPException(404, "Output file missing.")

    job_store.mark_downloaded(job_id)
    return FileResponse(
        path=job.output_path,
        filename=Path(job.output_path).name,
        media_type="application/octet-stream",
    )
