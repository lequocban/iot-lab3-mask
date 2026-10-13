import hashlib
import hmac
import io
import os
import threading
import time
import urllib.request
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from PIL import Image, ImageOps, UnidentifiedImageError
from starlette.concurrency import run_in_threadpool

from detector import Detector, NAMES
from alerts import AlertConfig, AlertService

BASE = Path(__file__).resolve().parent
Image.MAX_IMAGE_PIXELS = 12_000_000
LOCK = threading.Lock()
MAX_UPLOAD = 2 * 1024 * 1024


def load_model_file():
    path = Path(os.getenv("MODEL_PATH", str(BASE / "assets/model.onnx")))
    url = os.getenv("MODEL_URL", "")
    expected = os.getenv("MODEL_SHA256", "").lower()
    if url:
        if not url.startswith("https://") or len(expected) != 64:
            raise RuntimeError("MODEL_URL needs HTTPS and MODEL_SHA256 (64 hex characters)")
        path = Path(os.getenv("MODEL_CACHE_PATH", "model-cache.onnx"))
        part = path.with_suffix(".part")
        digest, size = hashlib.sha256(), 0
        try:
            with urllib.request.urlopen(url, timeout=90) as response, part.open("wb") as target:
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > 100 * 1024 * 1024:
                        raise RuntimeError("Model exceeds 100 MiB")
                    digest.update(chunk)
                    target.write(chunk)
            if digest.hexdigest() != expected:
                raise RuntimeError("Downloaded model SHA256 mismatch")
            part.replace(path)
        finally:
            part.unlink(missing_ok=True)
    if not path.is_file():
        raise RuntimeError(f"Missing model: {path}. Run train.py and copy model.onnx first.")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if expected and actual != expected:
        raise RuntimeError("Model SHA256 mismatch")
    return path, actual


@asynccontextmanager
async def lifespan(app):
    path, checksum = load_model_file()
    app.state.detector = Detector(path)
    app.state.checksum = checksum
    app.state.alerts = AlertService(AlertConfig.from_env())
    app.state.alerts.start()
    try:
        yield
    finally:
        await run_in_threadpool(app.state.alerts.close)


app = FastAPI(title="IoT Lab 3 - three mask labels", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "http://localhost:5500,http://127.0.0.1:5500").split(","),
    allow_methods=["GET", "POST"], allow_headers=["Content-Type", "X-API-Key", "X-Session-ID"],
)


@app.get("/")
def index():
    return FileResponse(BASE / "web/index.html")


@app.get("/health")
def health():
    return {"status": "ok", "classes": NAMES, "model_sha256": app.state.checksum,
            "input_size": app.state.detector.size, "provider": "CPUExecutionProvider"}


def authorize(key):
    expected = os.getenv("API_KEY", "")
    if expected and not hmac.compare_digest(expected, key or ""):
        raise HTTPException(401, "Invalid API key")


def validate_session(value):
    if value is None:
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise HTTPException(400, "X-Session-ID must be a UUID") from None


@app.get("/alerts/status")
def alert_status(session_id: str | None = None, x_api_key: str | None = Header(default=None)):
    authorize(x_api_key)
    return app.state.alerts.snapshot(validate_session(session_id))


@app.get("/favicon.svg", include_in_schema=False)
def favicon_svg():
    return FileResponse(BASE / "web/favicon.svg", media_type="image/svg+xml")


@app.get("/favicon.ico", include_in_schema=False)
def favicon_ico():
    return RedirectResponse("/favicon.svg", status_code=307)


def infer(data):
    start = time.perf_counter()
    try:
        with Image.open(io.BytesIO(data)) as source:
            if source.width * source.height > 12_000_000:
                raise HTTPException(413, "Image exceeds 12 megapixels")
            image = ImageOps.exif_transpose(source).convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise HTTPException(400, "Invalid/unsupported image data") from exc
    decoded = time.perf_counter()
    # Include time waiting for the inference lock in cloud_pipeline_ms.
    with LOCK:
        acquired = time.perf_counter()
        detections, timing = app.state.detector.predict(image)
    end = time.perf_counter()
    timing.update(decode_ms=(decoded - start) * 1000,
                  queue_ms=(acquired - decoded) * 1000,
                  cloud_pipeline_ms=(end - start) * 1000)
    return {"width": image.width, "height": image.height, "detections": detections,
            "timing": timing, "model_sha256": app.state.checksum}


@app.post("/predict")
async def predict(file: UploadFile = File(...), notify: bool = Form(default=False),
                  x_api_key: str | None = Header(default=None), x_session_id: str | None = Header(default=None)):
    authorize(x_api_key)
    session_id = validate_session(x_session_id)
    if file.content_type not in ("image/jpeg", "image/png", "image/webp"):
        raise HTTPException(415, "Send JPEG, PNG or WebP in multipart field 'file'")
    data = await file.read(MAX_UPLOAD + 1)
    await file.close()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "Upload exceeds 2 MiB")
    result = await run_in_threadpool(infer, data)
    result["alert"] = app.state.alerts.observe(session_id, result, data, notify)
    result["alert_status"] = app.state.alerts.snapshot(session_id)
    return result
