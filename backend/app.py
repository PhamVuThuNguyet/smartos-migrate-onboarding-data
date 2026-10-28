"""HTTP API for the preview UI. Next.js calls POST /preview."""

from datetime import date
from pathlib import Path
from tempfile import NamedTemporaryFile

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from import_api import preview_workbook
from onboarding import issue

app = FastAPI(title="SmartOS onboarding preview")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)


def rejected_file() -> dict:
    return {
        "asOf": date.today().isoformat(),
        "errors": [issue("E01", "File không đúng định dạng .xlsx")],
        "calls": [],
        "skipped": [],
        "error": "",
    }


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/preview")
async def preview_records(file: UploadFile = File(...)):
    content = await file.read()
    if not (file.filename or "").lower().endswith(".xlsx") or not content:
        return rejected_file()
    temporary = NamedTemporaryFile(suffix=".xlsx", delete=False)
    temporary.write(content)
    temporary.close()
    path = Path(temporary.name)
    try:
        return preview_workbook(path)
    except Exception as error:
        raise HTTPException(status_code=400, detail=f"Không đọc được file Excel: {error}") from error
    finally:
        path.unlink(missing_ok=True)
