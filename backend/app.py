"""HTTP API for the preview UI. Next.js calls POST /preview."""

from pathlib import Path
from tempfile import NamedTemporaryFile

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from import_api import preview_workbook

app = FastAPI(title="SmartOS onboarding preview")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)


async def save_upload(upload: UploadFile) -> Path:
    filename = (upload.filename or "").lower()
    if not filename.endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="Chỉ nhận file Excel .xlsx")
    content = await upload.read()
    if not content:
        raise HTTPException(status_code=400, detail="File trống")
    temporary = NamedTemporaryFile(suffix=".xlsx", delete=False)
    temporary.write(content)
    temporary.close()
    return Path(temporary.name)


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/preview")
async def preview_records(file: UploadFile = File(...)):
    path = await save_upload(file)
    try:
        return preview_workbook(path)
    except Exception as error:
        raise HTTPException(status_code=400, detail=f"Không đọc được file Excel: {error}") from error
    finally:
        path.unlink(missing_ok=True)
