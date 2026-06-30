"""本地存储：UUID 命名、路径穿越防护、每 job 独立 workdir、过期清理。规格 §17。"""
from __future__ import annotations

import shutil
import uuid
from datetime import timedelta
from pathlib import Path

from sqlmodel import Session, select

from app.config import settings
from app.errors import ConversionError, ErrorCode
from app.models import ConversionJob, FileRecord, _now

UPLOADS = "uploads"
RESULTS = "results"
WORKDIRS = "workdirs"


def _root() -> Path:
    return settings.storage_root.resolve()


def _safe_join(rel: str) -> Path:
    """拼接并校验路径必须落在 storage_root 内，防止路径穿越。"""
    p = (_root() / rel).resolve()
    try:
        p.relative_to(_root())
    except ValueError as exc:
        raise ConversionError(ErrorCode.CONVERSION_ENGINE_ERROR, "非法路径访问") from exc
    return p


def sanitize_filename(name: str) -> str:
    """仅保留文件名、去危险字符；用于展示/结果命名，永不直接作为存储路径。"""
    base = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    cleaned = "".join(c for c in base if c.isalnum() or c in (".", "-", "_", " ", "(", ")")).strip()
    return cleaned or "file"


def save_upload(content: bytes, source_ext: str) -> str:
    """以 UUID 命名保存上传文件，返回相对路径（uploads/<uuid><ext>）。"""
    rel, path = alloc_upload(source_ext)
    path.write_bytes(content)
    return rel


def alloc_upload(source_ext: str) -> tuple[str, Path]:
    """预分配上传落盘路径（相对, 绝对），供流式写入；失败时调用方需自行删除。"""
    settings.ensure_dirs()
    name = f"{uuid.uuid4().hex}{source_ext}"
    return f"{UPLOADS}/{name}", _safe_join(f"{UPLOADS}/{name}")


def upload_abspath(stored_rel: str) -> Path:
    return _safe_join(stored_rel)


def create_workdir(job_id: str) -> Path:
    settings.ensure_dirs()
    path = _safe_join(f"{WORKDIRS}/{job_id}")
    path.mkdir(parents=True, exist_ok=True)
    return path


def cleanup_workdir(job_id: str) -> None:
    path = _safe_join(f"{WORKDIRS}/{job_id}")
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def result_abspath(job_id: str, target_ext: str) -> Path:
    return _safe_join(f"{RESULTS}/{job_id}{target_ext}")


def delete_upload(stored_rel: str) -> None:
    path = _safe_join(stored_rel)
    if path.exists():
        path.unlink(missing_ok=True)


def delete_result(job_id: str, target_ext: str) -> None:
    path = result_abspath(job_id, target_ext)
    if path.exists():
        path.unlink(missing_ok=True)


def is_expired(expires_at) -> bool:  # noqa: ANN001
    """请求时过期判定：不依赖周期清理是否及时。"""
    return expires_at is not None and expires_at < _now()


def expire_file(session: Session, file: FileRecord) -> None:
    """事务性地把文件及其关联 job 置为 expired 并删除落盘文件。"""
    delete_upload(file.stored_path)
    file.status = "expired"
    session.add(file)
    for j in session.exec(select(ConversionJob).where(ConversionJob.file_id == file.id)).all():
        if j.status != "expired":
            delete_result(j.id, j.target_ext)
            j.status = "expired"
            session.add(j)
    session.commit()


def sweep_asr_tmp(max_age_hours: int | None = None) -> int:
    """清理 asr_tmp 下超期残留的临时音频（provider 正常会在 finally 删除；此为兜底）。"""
    import time

    root = _root() / "asr_tmp"
    if not root.exists():
        return 0
    cutoff = time.time() - (max_age_hours or settings.file_retention_hours) * 3600
    count = 0
    for p in root.iterdir():
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
                count += 1
        except OSError:
            continue
    return count


def sweep_expired(session: Session) -> int:
    """删除超过 FILE_RETENTION_HOURS 的上传/结果，并把记录标记 expired。返回清理条数。"""
    cutoff = _now() - timedelta(hours=settings.file_retention_hours)
    count = 0

    files = session.exec(select(FileRecord).where(FileRecord.status == "uploaded")).all()
    for f in files:
        if f.created_at < cutoff:
            delete_upload(f.stored_path)
            f.status = "expired"
            session.add(f)
            count += 1
            # 关联 job 置 expired
            jobs = session.exec(select(ConversionJob).where(ConversionJob.file_id == f.id)).all()
            for j in jobs:
                if j.status not in ("expired",):
                    if j.result_path:
                        delete_result(j.id, j.target_ext)
                    j.status = "expired"
                    session.add(j)

    session.commit()
    return count
