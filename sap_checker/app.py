"""SAP Entry Checker - web app.   Run:  python app.py   then open http://127.0.0.1:5000"""
import os
import shutil
import threading
import time
import uuid
import webbrowser

from flask import Flask, jsonify, render_template, request, send_from_directory
from werkzeug.utils import secure_filename

from core.excel_parser import load_requirements
from core.ocr_engine import engine_name, run_ocr
from core.pipeline import run_job

MAX_FILES = 40
MAX_IMG_MB = 15
IMG_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}
XLS_EXT = {".xlsx", ".xlsm"}

BASE = os.path.dirname(os.path.abspath(__file__))
JOBS_DIR = os.path.join(BASE, "jobs")
os.makedirs(JOBS_DIR, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = (MAX_FILES * MAX_IMG_MB + 30) * 1024 * 1024

JOBS = {}
JOBS_LOCK = threading.Lock()
RUN_LOCK = threading.Lock()          # one OCR job at a time -> predictable speed


def _cleanup_old_jobs(max_age_hours=6):
    now = time.time()
    with JOBS_LOCK:
        for jid in list(JOBS):
            if now - JOBS[jid]["created"] > max_age_hours * 3600:
                JOBS.pop(jid, None)
                shutil.rmtree(os.path.join(JOBS_DIR, jid), ignore_errors=True)


def _worker(job_id, files, excel_path, strict):
    job = JOBS[job_id]

    def progress(done, total, msg):
        job.update(done=done, total=total, message=msg)

    try:
        with RUN_LOCK:
            job.update(state="running", message="Loading OCR engine (first run can take a few seconds)...")
            result = run_job(files, excel_path, os.path.join(JOBS_DIR, job_id), strict, progress)
        job.update(state="done", result=result, message="Finished")
    except Exception as exc:                                    # surfaced to the UI
        job.update(state="error", error=f"{type(exc).__name__}: {exc}")


@app.errorhandler(413)
def too_large(_e):
    return jsonify(error=f"Upload too large. Max {MAX_FILES} screenshots of {MAX_IMG_MB} MB each."), 413


@app.route("/")
def index():
    return render_template("index.html", max_files=MAX_FILES, max_mb=MAX_IMG_MB)


@app.route("/api/start", methods=["POST"])
def start():
    _cleanup_old_jobs()
    images = [f for f in request.files.getlist("screenshots") if f and f.filename]
    excel = request.files.get("excel")
    strict = request.form.get("mode", "strict") != "lenient"

    if not excel or not excel.filename:
        return jsonify(error="Please upload the Excel file."), 400
    if os.path.splitext(excel.filename)[1].lower() not in XLS_EXT:
        return jsonify(error="The Excel file must be .xlsx or .xlsm"), 400
    if not images:
        return jsonify(error="Please upload at least one screenshot."), 400
    if len(images) > MAX_FILES:
        return jsonify(error=f"Maximum {MAX_FILES} screenshots allowed (you sent {len(images)})."), 400

    files, seen = [], set()
    for f in images:
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in IMG_EXT:
            return jsonify(error=f"'{f.filename}' is not a supported image (use JPG, PNG, BMP, WEBP, TIFF)."), 400
        data = f.read()
        if len(data) > MAX_IMG_MB * 1024 * 1024:
            return jsonify(error=f"'{f.filename}' is larger than {MAX_IMG_MB} MB."), 400
        if not data:
            return jsonify(error=f"'{f.filename}' is empty."), 400
        name = secure_filename(f.filename) or f"image{len(files) + 1}{ext}"
        base, n = name, 1
        while name.lower() in seen:
            n += 1
            name = f"{os.path.splitext(base)[0]}_{n}{ext}"
        seen.add(name.lower())
        files.append((name, data))

    job_id = uuid.uuid4().hex[:12]
    job_dir = os.path.join(JOBS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    excel_path = os.path.join(job_dir, "master" + os.path.splitext(excel.filename)[1].lower())
    excel.save(excel_path)
    try:
        reqs = load_requirements(excel_path)       # validate immediately
    except Exception as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify(error=f"Could not read the Excel file: {exc}"), 400

    with JOBS_LOCK:
        JOBS[job_id] = {"state": "queued", "done": 0, "total": len(files), "message": "Queued",
                        "created": time.time(), "result": None, "error": ""}
    threading.Thread(target=_worker, args=(job_id, files, excel_path, strict), daemon=True).start()
    return jsonify(job_id=job_id, rows=len(reqs), images=len(files))


@app.route("/api/status/<job_id>")
def status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify(error="Unknown or expired job"), 404
    return jsonify(state=job["state"], done=job["done"], total=job["total"],
                   message=job["message"], error=job["error"])


@app.route("/api/result/<job_id>")
def result(job_id):
    job = JOBS.get(job_id)
    if not job or job["state"] != "done":
        return jsonify(error="Result not ready"), 404
    return jsonify(job["result"])


@app.route("/files/<job_id>/<path:name>")
def job_file(job_id, name):
    if job_id not in JOBS:
        return "Not found", 404
    return send_from_directory(os.path.join(JOBS_DIR, job_id), name)


@app.route("/download/<job_id>")
def download(job_id):
    if job_id not in JOBS:
        return "Not found", 404
    return send_from_directory(os.path.join(JOBS_DIR, job_id), "SAP_Entry_Check_Report.xlsx",
                               as_attachment=True)


def _warm_up():
    """Load the OCR model in the background so the first job starts fast."""
    try:
        import numpy as np
        run_ocr(np.full((64, 256, 3), 255, dtype=np.uint8))
        print(f"OCR engine ready: {engine_name()}")
    except Exception as exc:
        print("WARNING: OCR engine failed to load:", exc)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    threading.Thread(target=_warm_up, daemon=True).start()
    threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    print(f"Open http://127.0.0.1:{port}  (Ctrl+C to stop)")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
