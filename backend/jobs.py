"""File-backed import jobs: queue, progress, checkpoint/resume."""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

JOBS_DIR = Path(__file__).resolve().parent / ".jobs"
JOBS_DIR.mkdir(exist_ok=True)

_lock = threading.Lock()
_workers: dict[str, threading.Thread] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.json"


def _xlsx_path(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.xlsx"


def create_job(
    *,
    domain: str,
    access_token: str,
    refresh_token: str,
    filename: str,
    content: bytes,
) -> dict:
    job_id = uuid.uuid4().hex
    _xlsx_path(job_id).write_bytes(content)
    job = {
        "id": job_id,
        "status": "queued",
        "phase": "queued",
        "message": "Đang chờ chạy",
        "filename": filename,
        "domain": domain,
        "accessToken": access_token,
        "refreshToken": refresh_token or "",
        "createdAt": _now(),
        "updatedAt": _now(),
        "totalBookings": 0,
        "doneBookings": 0,
        "percent": 0,
        "completedKeys": [],
        "recentSteps": [],
        "error": "",
        "result": None,
    }
    save_job(job)
    return public_job(job)


def load_job(job_id: str) -> dict | None:
    path = _path(job_id)
    if not path.exists():
        return None
    with _lock:
        return json.loads(path.read_text(encoding="utf-8"))


def save_job(job: dict) -> None:
    job = dict(job)
    job["updatedAt"] = _now()
    path = _path(job["id"])
    tmp = path.with_suffix(".tmp")
    with _lock:
        tmp.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)


def public_job(job: dict) -> dict:
    """Strip secrets for API responses."""
    return {
        "id": job["id"],
        "status": job.get("status"),
        "phase": job.get("phase"),
        "message": job.get("message") or "",
        "filename": job.get("filename") or "",
        "createdAt": job.get("createdAt"),
        "updatedAt": job.get("updatedAt"),
        "totalBookings": job.get("totalBookings") or 0,
        "doneBookings": job.get("doneBookings") or 0,
        "percent": job.get("percent") or 0,
        "completedKeys": job.get("completedKeys") or [],
        "recentSteps": job.get("recentSteps") or [],
        "error": job.get("error") or "",
        "result": job.get("result"),
    }


def update_progress(
    job_id: str,
    *,
    status: str | None = None,
    phase: str | None = None,
    message: str | None = None,
    total_bookings: int | None = None,
    done_bookings: int | None = None,
    completed_keys: list[str] | None = None,
    recent_steps: list[dict] | None = None,
    error: str | None = None,
    result: Any = None,
    percent: int | None = None,
) -> dict | None:
    job = load_job(job_id)
    if not job:
        return None
    if status is not None:
        job["status"] = status
    if phase is not None:
        job["phase"] = phase
    if message is not None:
        job["message"] = message
    if total_bookings is not None:
        job["totalBookings"] = total_bookings
    if done_bookings is not None:
        job["doneBookings"] = done_bookings
    if completed_keys is not None:
        job["completedKeys"] = completed_keys
    if recent_steps is not None:
        job["recentSteps"] = recent_steps[-40:]
    if error is not None:
        job["error"] = error
    if result is not None:
        job["result"] = result
    if percent is not None:
        job["percent"] = max(0, min(100, int(percent)))
    elif job.get("totalBookings"):
        job["percent"] = int(100 * (job.get("doneBookings") or 0) / job["totalBookings"])
    save_job(job)
    return job


def start_worker(job_id: str, runner: Callable[[str], None]) -> None:
    with _lock:
        existing = _workers.get(job_id)
        if existing and existing.is_alive():
            return

        def target():
            try:
                runner(job_id)
            finally:
                with _lock:
                    _workers.pop(job_id, None)

        thread = threading.Thread(target=target, name=f"import-job-{job_id[:8]}", daemon=True)
        _workers[job_id] = thread
        thread.start()


def workbook_path(job_id: str) -> Path:
    return _xlsx_path(job_id)


def cleanup_workbook(job_id: str) -> None:
    path = _xlsx_path(job_id)
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass
