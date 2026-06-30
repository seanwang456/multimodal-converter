"""POST /api/files —— 上传 + 三重校验（扩展名/MIME/大小，流式写盘不撑爆内存）。规格 §9.1。"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, File, UploadFile
from sqlmodel import Session

from app.database import engine
from app.errors import ConversionError, ErrorCode
from app.filetypes import SUPPORTED_EXTS, max_bytes_for, mime_consistent, normalize_ext
from app.models import FileRecord, new_file_id, _now
from app.services import registry, storage
from app.config import settings

router = APIRouter(prefix="/api/files", tags=["files"])

_CHUNK = 1024 * 1024  # 1 MiB


def _allowed_targets(source_ext: str) -> list[str]:
    """计算合法目标；可上传但暂不可转换（如 Phase 1 的 .pdf）返回空列表，不抛错。"""
    try:
        return [r["target_ext"] for r in registry.get_targets(source_ext)]
    except ConversionError:
        return []


@router.post("")
def upload_file(file: UploadFile = File(...)) -> dict:
    original = file.filename or "file"
    source_ext = normalize_ext(original)

    if source_ext not in SUPPORTED_EXTS:
        raise ConversionError(ErrorCode.UNSUPPORTED_FILE_TYPE, "当前文件格式暂不支持")

    limit = max_bytes_for(source_ext)
    if not mime_consistent(file.content_type or "", source_ext):
        raise ConversionError(ErrorCode.UNSUPPORTED_FILE_TYPE, "文件类型与扩展名不一致")

    # 先计算目标（不依赖落盘），再流式写盘；超限即截断并清理，避免大文件全量入内存
    allowed_targets = _allowed_targets(source_ext)
    stored_rel, abs_path = storage.alloc_upload(source_ext)
    size = _stream_to(file, abs_path, limit, stored_rel)

    now = _now()
    record = FileRecord(
        id=new_file_id(),
        original_filename=storage.sanitize_filename(original),
        stored_path=stored_rel,
        source_ext=source_ext,
        mime_type=file.content_type or "application/octet-stream",
        size_bytes=size,
        status="uploaded",
        created_at=now,
        expires_at=now + timedelta(hours=settings.file_retention_hours),
    )
    try:
        with Session(engine) as s:
            s.add(record)
            s.commit()
            s.refresh(record)
    except Exception:
        storage.delete_upload(stored_rel)  # 不留孤儿
        raise

    return {
        "file_id": record.id,
        "filename": record.original_filename,
        "source_ext": record.source_ext,
        "mime_type": record.mime_type,
        "size_bytes": record.size_bytes,
        "allowed_targets": allowed_targets,
        "created_at": record.created_at.isoformat(),
    }


def _stream_to(file: UploadFile, dest: Path, limit: int | None, stored_rel: str) -> int:
    """流式分块写盘；任意失败（含超限/空/IO 异常）都清理 partial 文件。返回写入字节数。"""
    total = 0
    try:
        with open(dest, "wb") as out:
            while True:
                chunk = file.file.read(_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if limit is not None and total > limit:
                    mb = limit // (1024 * 1024)
                    raise ConversionError(ErrorCode.FILE_TOO_LARGE, f"文件超过 {mb}MB 限制")
                out.write(chunk)
    except Exception:
        storage.delete_upload(stored_rel)  # 不留 partial 孤儿
        raise
    if total == 0:
        storage.delete_upload(stored_rel)
        raise ConversionError(ErrorCode.EMPTY_FILE, "文件为空，请重新上传")
    return total
