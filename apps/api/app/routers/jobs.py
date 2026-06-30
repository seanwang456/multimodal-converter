"""POST /api/jobs、GET /api/jobs/{id}、GET /api/jobs/{id}/download。规格 §9.3-9.5。"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlmodel import Session

from app.database import engine
from app.errors import ConversionError, ErrorCode
from app.models import ConversionJob, FileRecord, new_job_id
from app.services import job_runner, registry, storage

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


class CreateJobRequest(BaseModel):
    file_id: str
    target_ext: str
    options: dict[str, Any] = {}
    # 故意接受但忽略：后端为唯一校验源，handler_key 由 registry 自定
    handler_key: str | None = None


def _load_file(session: Session, file_id: str) -> FileRecord:
    file = session.get(FileRecord, file_id)
    if not file:
        raise ConversionError(ErrorCode.NOT_FOUND, "文件不存在")
    if file.status == "expired" or storage.is_expired(file.expires_at):
        storage.expire_file(session, file)
        raise ConversionError(ErrorCode.FILE_EXPIRED, "文件已过期，请重新上传")
    return file


@router.post("")
def create_job(req: CreateJobRequest) -> dict:
    target_ext = req.target_ext.lower()
    with Session(engine) as s:
        file = _load_file(s, req.file_id)
        handler_key = registry.resolve_handler_key(file.source_ext, target_ext)
        job = ConversionJob(
            id=new_job_id(),
            file_id=file.id,
            source_ext=file.source_ext,
            target_ext=target_ext,
            handler_key=handler_key,
            status="queued",
            options=json.dumps(req.options),
        )
        s.add(job)
        s.commit()
        s.refresh(job)
        job_id = job.id

    job_runner.get_runner().submit(job_id)
    return {"job_id": job_id, "status": "queued"}


@router.get("/{job_id}")
def get_job(job_id: str) -> dict:
    with Session(engine) as s:
        job = s.get(ConversionJob, job_id)
        if not job:
            raise ConversionError(ErrorCode.NOT_FOUND, "任务不存在")
        file = s.get(FileRecord, job.file_id)
        # 请求时过期：文件已过期则连带 job 标记 expired
        if file and job.status != "expired" and storage.is_expired(file.expires_at):
            storage.expire_file(s, file)
            s.refresh(job)
        result = None
        if job.status == "succeeded":
            result = {
                "download_url": f"/api/jobs/{job.id}/download",
                "filename": job.result_filename,
                "size_bytes": job.result_size_bytes,
                "quality_notice": job.quality_notice,
            }
        error = None
        if job.status == "failed":
            error = {"code": job.error_code, "message": job.error_message}
        return {
            "job_id": job.id,
            "status": job.status,
            "progress": job.progress,
            "message": job.message,
            "source_file": {
                "filename": file.original_filename if file else None,
                "source_ext": job.source_ext,
            },
            "target_ext": job.target_ext,
            "result": result,
            "error": error,
        }


@router.get("/{job_id}/download")
def download(job_id: str) -> FileResponse:
    with Session(engine) as s:
        job = s.get(ConversionJob, job_id)
        if not job:
            raise ConversionError(ErrorCode.NOT_FOUND, "任务不存在")
        if job.status == "expired":
            raise ConversionError(ErrorCode.FILE_EXPIRED, "文件已过期，请重新上传")
        file = s.get(FileRecord, job.file_id)
        if file and storage.is_expired(file.expires_at):
            storage.expire_file(s, file)
            raise ConversionError(ErrorCode.FILE_EXPIRED, "文件已过期，请重新上传")
        if job.status != "succeeded":
            raise ConversionError(ErrorCode.DOWNLOAD_NOT_READY, "转换尚未完成，请稍后下载")
    path = storage.result_abspath(job_id, job.target_ext)
    if not path.exists():
        raise ConversionError(ErrorCode.FILE_EXPIRED, "结果文件已过期")
    return FileResponse(str(path), filename=job.result_filename)
