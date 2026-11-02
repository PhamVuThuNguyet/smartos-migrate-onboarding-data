"""HTTP API for the preview UI. Next.js calls POST /preview, /import and /login."""

from datetime import date
from pathlib import Path
from tempfile import NamedTemporaryFile

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from identity import IdentityError, login, logout
from import_api import SmartOSError, import_workbook, preview_workbook
from onboarding import issue

app = FastAPI(title="SmartOS onboarding preview")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
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
):
    if not domain.strip() or not accessToken.strip():
        raise HTTPException(status_code=401, detail="Cần đăng nhập trước khi ghi dữ liệu")
    content = await file.read()
    if not (file.filename or "").lower().endswith(".xlsx") or not content:
        return rejected_file()
    path = save_upload(file, content)
    try:
        return import_workbook(path, domain, accessToken, refreshToken)
    except SmartOSError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=400, detail=f"Không ghi được dữ liệu: {error}") from error
    finally:
        path.unlink(missing_ok=True)
