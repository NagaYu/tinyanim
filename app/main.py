"""TinyAnim — FastAPI application.

Responsibilities
----------------
* Serve the single-page front end.
* Expose ``/api/optimize`` (upload), ``/api/download/{id}`` and ``/api/stats``.
* Enforce production safeguards: strict extension whitelist, streamed size cap
  (no full-buffer memory blow-ups), UUID-only download routing (no path
  traversal) and automatic purge of stale temp files.
"""

from __future__ import annotations

import asyncio
import io
import os
import re
import threading
import time
import uuid
import zipfile
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

from fastapi import (
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from . import auth, billing
from .database import DATA_DIR, get_db, init_db
from .models import ApiKey, GlobalStat, OptimizationRecord, ProcessedEvent
from .optimizer import optimize_bytes
from .plans import PLANS, get_plan

# --------------------------------------------------------------------------- #
#  Configuration
# --------------------------------------------------------------------------- #
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
# DATA_DIR is env-configurable (persistent disk on Render/Railway).
STORAGE_DIR = DATA_DIR / "storage"
STORAGE_DIR.mkdir(parents=True, exist_ok=True)

ANON_MAX_UPLOAD_BYTES = 10 * 1024 * 1024     # anonymous per-file cap (10 MB)
CHUNK_SIZE = 64 * 1024                        # streamed read granularity
FILE_TTL_SECONDS = 24 * 60 * 60              # purge optimized files after 24h
PURGE_INTERVAL_SECONDS = 60 * 60            # run the purge sweep hourly
_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# Issuing API keys requires this shared secret (sent as the X-Admin-Token header).
ADMIN_TOKEN = os.environ.get("TINYANIM_ADMIN_TOKEN")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")

# Per-IP rate limit for anonymous use of the optimize endpoint.
RATE_LIMIT = int(os.environ.get("TINYANIM_RATE_LIMIT", "60"))  # requests / minute
_RATE_WINDOW = 60.0
_rate_hits: dict[str, deque] = {}
_rate_lock = threading.Lock()

# ext -> internal optimizer type (upload whitelist)
ALLOWED_EXTENSIONS = {".json": "lottie", ".svg": "svg"}
# ext -> mime, for downloads (includes batch .zip)
DOWNLOAD_MIME = {
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".zip": "application/zip",
}

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    _purge_stale_files()
    task = asyncio.create_task(_purge_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(
    title="TinyAnim",
    description="Lottie & SVG ultra-compressor",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- #
#  Lifecycle / housekeeping
# --------------------------------------------------------------------------- #
def _purge_stale_files() -> None:
    cutoff = time.time() - FILE_TTL_SECONDS
    for path in STORAGE_DIR.glob("*"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


async def _purge_loop() -> None:
    """Hourly sweep so stale temp files never accumulate on a long-lived process."""
    while True:
        try:
            await asyncio.sleep(PURGE_INTERVAL_SECONDS)
            _purge_stale_files()
            _prune_rate_map()
        except asyncio.CancelledError:
            break
        except Exception:  # noqa: BLE001 — housekeeping must never crash the loop
            pass


def _prune_rate_map() -> None:
    now = time.time()
    with _rate_lock:
        for ip in list(_rate_hits):
            dq = _rate_hits[ip]
            while dq and dq[0] <= now - _RATE_WINDOW:
                dq.popleft()
            if not dq:
                del _rate_hits[ip]


# --------------------------------------------------------------------------- #
#  Rate limiting (in-memory, per IP) — protects the optimize endpoint
# --------------------------------------------------------------------------- #
@app.middleware("http")
async def _rate_limit(request: Request, call_next):
    has_key = bool(
        request.headers.get("x-api-key") or request.headers.get("authorization")
    )
    # API-key traffic is governed by per-key quota, not the anonymous IP limit.
    if request.url.path == "/api/optimize" and request.method == "POST" and not has_key:
        fwd = request.headers.get("x-forwarded-for", "")
        ip = fwd.split(",")[0].strip() or (
            request.client.host if request.client else "unknown"
        )
        now = time.time()
        with _rate_lock:
            dq = _rate_hits.setdefault(ip, deque())
            while dq and dq[0] <= now - _RATE_WINDOW:
                dq.popleft()
            if len(dq) >= RATE_LIMIT:
                return JSONResponse(
                    {"detail": "Rate limit exceeded. Please slow down and retry shortly."},
                    status_code=429,
                )
            dq.append(now)
    return await call_next(request)


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #
async def _read_capped(upload: UploadFile, max_bytes: int) -> bytes:
    """Read an upload in chunks, aborting as soon as the cap is exceeded."""
    size = 0
    chunks: list[bytes] = []
    while True:
        chunk = await upload.read(CHUNK_SIZE)
        if not chunk:
            break
        size += len(chunk)
        if size > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"File exceeds the {max_bytes // (1024 * 1024)} MB limit for your plan.",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _kb(num_bytes: int) -> float:
    return round(num_bytes / 1024, 2)


def _max_upload_bytes(key: Optional[ApiKey]) -> int:
    if key is None:
        return ANON_MAX_UPLOAD_BYTES
    return get_plan(key.plan).max_upload_mb * 1024 * 1024


def _process(filename: str, contents: bytes) -> tuple[str, str, bytes]:
    """Validate + optimize one file. Returns (file_type, ext, optimized_bytes).

    Raises ``ValueError`` for an unsupported extension and any optimizer error
    for a corrupt file (callers translate these into HTTP responses)."""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(f"Unsupported file type: {ext or '(none)'}")
    file_type = ALLOWED_EXTENSIONS[ext]
    optimized = optimize_bytes(file_type, contents)
    if len(optimized) >= len(contents):  # never inflate
        optimized = contents
    return file_type, ext, optimized


def _store(file_id: str, ext: str, data: bytes) -> None:
    (STORAGE_DIR / f"{file_id}{ext}").write_bytes(data)


# --------------------------------------------------------------------------- #
#  Routes
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((TEMPLATES_DIR / "index.html").read_text(encoding="utf-8"))


@app.post("/api/optimize")
async def optimize(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    key: Optional[ApiKey] = Depends(auth.optional_api_key),
):
    filename = file.filename or ""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail="Unsupported file type. Upload a Lottie (.json) or .svg file.",
        )

    contents = await _read_capped(file, _max_upload_bytes(key))
    if not contents:
        raise HTTPException(status_code=400, detail="Empty file.")

    # Reserve quota *before* doing the work so an over-quota key spends no CPU.
    if key is not None:
        auth.consume_quota(db, key, 1)

    original_size = len(contents)
    try:
        file_type, ext, optimized = _process(filename, contents)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:  # noqa: BLE001 — any parse error means a bad/corrupt file
        raise HTTPException(
            status_code=422,
            detail="The file could not be parsed. Is it a valid Lottie/SVG file?",
        )
    optimized_size = len(optimized)

    file_id = uuid.uuid4().hex
    _store(file_id, ext, optimized)
    _record(db, file_id, file_type, original_size, optimized_size)

    saved = original_size - optimized_size
    saved_pct = round(saved / original_size * 100, 1) if original_size else 0.0
    payload = {
        "file_id": file_id,
        "file_type": file_type,
        "original_filename": os.path.basename(filename),
        "original_size_kb": _kb(original_size),
        "optimized_size_kb": _kb(optimized_size),
        "saved_bytes": saved,
        "saved_percentage": saved_pct,
        "download_url": f"/api/download/{file_id}",
    }
    if key is not None:
        payload["quota_remaining"] = auth.quota_remaining(key)
    return payload


@app.get("/api/download/{file_id}")
def download(file_id: str) -> FileResponse:
    if not _ID_RE.match(file_id):
        raise HTTPException(status_code=400, detail="Invalid file id.")
    # file_id is a validated 32-char hex string, so this glob is traversal-safe.
    matches = sorted(STORAGE_DIR.glob(f"{file_id}.*"))
    if not matches:
        raise HTTPException(status_code=404, detail="File not found or expired.")
    path = matches[0]
    suffix = path.suffix.lower()
    return FileResponse(
        path,
        media_type=DOWNLOAD_MIME.get(suffix, "application/octet-stream"),
        filename=f"tinyanim-optimized{suffix}",
    )


@app.get("/api/stats")
def stats(db: Session = Depends(get_db)):
    stat = db.get(GlobalStat, 1) or GlobalStat(id=1)
    return {
        "total_files": stat.total_files or 0,
        "total_original_kb": _kb(stat.total_original_bytes or 0),
        "total_optimized_kb": _kb(stat.total_optimized_bytes or 0),
        "total_saved_kb": _kb(stat.total_saved_bytes or 0),
        "total_saved_mb": round((stat.total_saved_bytes or 0) / (1024 * 1024), 2),
    }


# --------------------------------------------------------------------------- #
#  Billing / API keys
# --------------------------------------------------------------------------- #
@app.get("/api/plans")
def list_plans():
    """Public pricing data (drives the pricing UI and your checkout buttons)."""
    return {
        "plans": [
            {
                "name": p.name,
                "price_usd": p.price_usd,
                "monthly_quota": p.monthly_quota,
                "batch_max_files": p.batch_max_files,
                "max_upload_mb": p.max_upload_mb,
            }
            for p in PLANS.values()
        ]
    }


@app.post("/api/keys", status_code=201)
async def create_key(
    request: Request,
    x_admin_token: Optional[str] = Header(None, alias="X-Admin-Token"),
    db: Session = Depends(get_db),
):
    """Issue a new API key. Protected by the admin token.

    A real billing flow calls this from your Stripe ``checkout.session.completed``
    webhook handler with the purchased ``plan``."""
    auth.verify_admin(x_admin_token or "", ADMIN_TOKEN)

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 — tolerate empty/missing body
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    plan = (payload.get("plan") or "free").lower()
    if plan not in PLANS:
        raise HTTPException(status_code=400, detail=f"Unknown plan '{plan}'.")
    label = str(payload.get("label") or "")[:120]

    raw = auth.generate_key()
    record = ApiKey(key_hash=auth.hash_key(raw), plan=plan, label=label)
    db.add(record)
    db.commit()
    return {
        "api_key": raw,  # shown exactly once — store it now
        "plan": plan,
        "label": label,
        "monthly_quota": get_plan(plan).monthly_quota,
        "note": "Store this key securely. It cannot be retrieved again.",
    }


@app.get("/api/me")
def me(key: ApiKey = Depends(auth.require_api_key)):
    """Return the calling key's plan and remaining quota."""
    plan = get_plan(key.plan)
    return {
        "plan": plan.name,
        "label": key.label,
        "monthly_quota": plan.monthly_quota,
        "used_this_period": key.used_this_period or 0,
        "quota_remaining": auth.quota_remaining(key),
        "batch_max_files": plan.batch_max_files,
        "max_upload_mb": plan.max_upload_mb,
        "period_start": key.period_start.isoformat() if key.period_start else None,
    }


@app.post("/api/checkout")
async def create_checkout(request: Request, db: Session = Depends(get_db)):
    """Start a paid subscription.

    Generates an **inactive** key now, returns the raw value once, and hands back
    a Stripe Checkout URL. The webhook activates the key after payment succeeds."""
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    plan = (payload.get("plan") or "").lower()
    if plan not in PLANS or plan == "free":
        raise HTTPException(status_code=400, detail="Choose a paid plan: 'pro' or 'business'.")

    base = str(request.base_url).rstrip("/")
    success_url = payload.get("success_url") or f"{base}/?checkout=success"
    cancel_url = payload.get("cancel_url") or f"{base}/?checkout=cancelled"
    email = (payload.get("email") or "")[:120] or None

    raw = auth.generate_key()
    record = ApiKey(key_hash=auth.hash_key(raw), plan=plan, label=email or "", active=False)
    db.add(record)
    db.commit()

    try:
        checkout_url = billing.create_checkout_session(
            plan, record.key_hash, success_url, cancel_url, email
        )
    except billing.BillingError as exc:
        # Roll back the dangling inactive key if Stripe couldn't be reached.
        db.delete(record)
        db.commit()
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)

    return {
        "checkout_url": checkout_url,
        "api_key": raw,
        "plan": plan,
        "note": "Save this key now. It activates automatically once payment completes.",
    }


@app.post("/api/webhooks/stripe")
async def stripe_webhook(
    request: Request,
    stripe_signature: Optional[str] = Header(None, alias="Stripe-Signature"),
    db: Session = Depends(get_db),
):
    """Receive Stripe events: verify signature, dedupe, then apply to the DB."""
    payload = await request.body()
    try:
        event = billing.verify_signature(payload, stripe_signature or "", STRIPE_WEBHOOK_SECRET)
    except billing.BillingError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)

    event_id = event.get("id")
    if event_id and db.get(ProcessedEvent, event_id) is not None:
        return {"received": True, "status": "duplicate"}

    status = billing.handle_event(db, event)

    if event_id:
        db.add(ProcessedEvent(event_id=event_id))
        db.commit()
    return {"received": True, "status": status}


@app.post("/api/batch")
async def batch(
    files: List[UploadFile] = File(...),
    db: Session = Depends(get_db),
    key: ApiKey = Depends(auth.require_api_key),
):
    """Optimize many files at once and return a single downloadable ZIP.

    A paid feature: the calling plan must allow batch, and the request may not
    exceed its ``batch_max_files``. Only successfully optimized files count
    against quota."""
    plan = get_plan(key.plan)
    if plan.batch_max_files <= 0:
        raise HTTPException(
            status_code=402,
            detail=f"Batch processing is not available on the '{plan.name}' plan.",
        )
    if not files:
        raise HTTPException(status_code=400, detail="No files provided.")
    if len(files) > plan.batch_max_files:
        raise HTTPException(
            status_code=413,
            detail=f"Batch exceeds the {plan.batch_max_files}-file limit for your plan.",
        )

    cap = _max_upload_bytes(key)
    results = []
    total_original = total_optimized = succeeded = 0
    buffer = io.BytesIO()

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        used_names: set[str] = set()
        for upload in files:
            name = os.path.basename(upload.filename or "file")
            contents = await _read_capped(upload, cap)
            entry = {"filename": name, "original_size_kb": _kb(len(contents))}
            try:
                _, ext, optimized = _process(name, contents)
            except Exception:  # noqa: BLE001 — skip bad files, keep the batch going
                entry["error"] = "Unsupported or corrupt file; skipped."
                results.append(entry)
                continue

            arcname = _unique_name(f"optimized-{name}", used_names)
            archive.writestr(arcname, optimized)

            saved = len(contents) - len(optimized)
            entry.update(
                optimized_size_kb=_kb(len(optimized)),
                saved_percentage=round(saved / len(contents) * 100, 1) if contents else 0.0,
            )
            results.append(entry)
            total_original += len(contents)
            total_optimized += len(optimized)
            succeeded += 1

    if succeeded == 0:
        raise HTTPException(status_code=422, detail="No valid Lottie/SVG files in the batch.")

    # Charge quota for what actually succeeded, then persist the archive + stats.
    auth.consume_quota(db, key, succeeded)
    file_id = uuid.uuid4().hex
    _store(file_id, ".zip", buffer.getvalue())
    _record(db, file_id, "batch", total_original, total_optimized)

    saved = total_original - total_optimized
    return {
        "files_processed": succeeded,
        "files_skipped": len(files) - succeeded,
        "total_original_kb": _kb(total_original),
        "total_optimized_kb": _kb(total_optimized),
        "saved_percentage": round(saved / total_original * 100, 1) if total_original else 0.0,
        "download_url": f"/api/download/{file_id}",
        "quota_remaining": auth.quota_remaining(key),
        "results": results,
    }


def _unique_name(name: str, used: set[str]) -> str:
    if name not in used:
        used.add(name)
        return name
    stem, ext = os.path.splitext(name)
    i = 1
    while f"{stem}-{i}{ext}" in used:
        i += 1
    final = f"{stem}-{i}{ext}"
    used.add(final)
    return final


# --------------------------------------------------------------------------- #
#  Persistence
# --------------------------------------------------------------------------- #
def _record(
    db: Session, file_id: str, file_type: str, original: int, optimized: int
) -> None:
    saved = original - optimized
    stat = db.get(GlobalStat, 1)
    if stat is None:
        stat = GlobalStat(id=1)
        db.add(stat)
    stat.total_files = (stat.total_files or 0) + 1
    stat.total_original_bytes = (stat.total_original_bytes or 0) + original
    stat.total_optimized_bytes = (stat.total_optimized_bytes or 0) + optimized
    stat.total_saved_bytes = (stat.total_saved_bytes or 0) + saved

    db.add(
        OptimizationRecord(
            file_id=file_id,
            file_type=file_type,
            original_bytes=original,
            optimized_bytes=optimized,
        )
    )
    db.commit()
