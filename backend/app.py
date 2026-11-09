"""HTTP API for the preview UI. Next.js calls POST /preview, /import and /login."""

import os
from datetime import date
from pathlib import Path
from tempfile import NamedTemporaryFile

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from identity import IdentityError, login, logout
from import_api import SmartOSError, import_workbook, preview_workbook, run_import_job
from jobs import create_job, load_job, public_job, start_worker, update_progress
from onboarding import issue

CORS_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://201.79.109.197",
    "https://201.79.109.197",
]
for origin in (os.environ.get("CORS_ORIGINS") or "").split(","):
    origin = origin.strip()
    if origin and origin not in CORS_ORIGINS:
        CORS_ORIGINS.append(origin)

app = FastAPI(title="SmartOS onboarding preview")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["*"],
)


def rejected_file() -> dict:
    return {
        "asOf": date.today().isoformat(),
        "errors": [issue("E01", "File không đúng định dạng .xlsx")],
        "calls": [],
        "skipped": [],
        "error": "",
        "steps": [],
        "imported": False,
        "summary": {"created": {}, "reused": {}, "vacantRooms": 0},
    }


class LoginBody(BaseModel):
    domain: str
    username: str
    password: str


class LogoutBody(BaseModel):
    domain: str = ""
    accessToken: str = ""


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/login")
def login_pms(body: LoginBody):
    try:
        return login(body.domain, body.username, body.password)
    except IdentityError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error


@app.post("/logout")
def logout_pms(body: LogoutBody):
    logout(body.domain, body.accessToken)
    return {"ok": True}


def save_upload(file: UploadFile, content: bytes) -> Path:
    temporary = NamedTemporaryFile(suffix=".xlsx", delete=False)
    temporary.write(content)
    temporary.close()
    return Path(temporary.name)


@app.post("/preview")
async def preview_records(
    file: UploadFile = File(...),
    domain: str = Form(""),
    accessToken: str = Form(""),
    refreshToken: str = Form(""),
):
    content = await file.read()
    if not (file.filename or "").lower().endswith(".xlsx") or not content:
        return rejected_file()
    path = save_upload(file, content)
    try:
        return preview_workbook(path, domain, accessToken, refreshToken)
    except SmartOSError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=400, detail=f"Không đọc được file Excel: {error}") from error
    finally:
        path.unlink(missing_ok=True)


@app.post("/import")
async def import_records(
    file: UploadFile = File(...),
    domain: str = Form(""),
    accessToken: str = Form(""),
    refreshToken: str = Form(""),
    sync: str = Form(""),
):
    """Start background import job (default). Pass sync=1 for legacy blocking import."""
    if not domain.strip() or not accessToken.strip():
        raise HTTPException(status_code=401, detail="Cần đăng nhập trước khi ghi dữ liệu")
    content = await file.read()
    if not (file.filename or "").lower().endswith(".xlsx") or not content:
        return rejected_file()

    if str(sync).strip() in {"1", "true", "yes"}:
        path = save_upload(file, content)
        try:
            return import_workbook(path, domain, accessToken, refreshToken)
        except SmartOSError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except Exception as error:
            raise HTTPException(status_code=400, detail=f"Không ghi được dữ liệu: {error}") from error
        finally:
            path.unlink(missing_ok=True)

    job = create_job(
        domain=domain.strip(),
        access_token=accessToken.strip(),
        refresh_token=(refreshToken or "").strip(),
        filename=file.filename or "import.xlsx",
        content=content,
    )
    start_worker(job["id"], run_import_job)
    return {"jobId": job["id"], "status": job["status"], "async": True}


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    job = load_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Không tìm thấy job")
    return public_job(job)


@app.post("/jobs/{job_id}/resume")
def resume_job(job_id: str):
    """Resume a failed/interrupted job from completedKeys checkpoint."""
    job = load_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Không tìm thấy job")
    if job.get("status") == "running":
        return public_job(job)
    if job.get("status") == "done":
        return public_job(job)
    from jobs import workbook_path

    if not workbook_path(job_id).exists():
        raise HTTPException(
            status_code=400,
            detail="File job đã bị xóa — upload lại để import. Checkpoint chỉ dùng khi file còn trên server.",
        )
    update_progress(job_id, status="queued", phase="queued", message="Resume từ checkpoint", error="")
    start_worker(job_id, run_import_job)
    job = load_job(job_id)
    return public_job(job or {})
