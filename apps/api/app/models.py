"""数据模型：FileRecord + ConversionJob。字段对齐规格 §10。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


def _now() -> datetime:
    """统一使用 naive UTC。SQLite 读回的是 naive datetime，比较前必须保持一致。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_file_id() -> str:
    return f"file_{uuid.uuid4().hex}"


def new_job_id() -> str:
    return f"job_{uuid.uuid4().hex}"


# 任务状态机：queued|running|succeeded|failed|expired
JOB_STATUSES = {"queued", "running", "succeeded", "failed", "expired"}
FILE_STATUSES = {"uploaded", "deleted", "expired"}


class FileRecord(SQLModel, table=True):
    __tablename__ = "file_records"

    id: str = Field(primary_key=True)  # file_xxx
    original_filename: str
    stored_path: str  # 仅服务端可见，绝不回传前端
    source_ext: str
    mime_type: str
    size_bytes: int
    status: str = Field(default="uploaded")  # uploaded|deleted|expired
    created_at: datetime = Field(default_factory=_now)
    expires_at: datetime


class ConversionJob(SQLModel, table=True):
    __tablename__ = "conversion_jobs"

    id: str = Field(primary_key=True)  # job_xxx
    file_id: str = Field(foreign_key="file_records.id")
    source_ext: str
    target_ext: str
    handler_key: str
    status: str = Field(default="queued")  # queued|running|succeeded|failed|expired
    progress: int = Field(default=0)
    message: str | None = Field(default=None)
    options: str = Field(default="{}")  # JSON 文本
    result_path: str | None = Field(default=None)  # 仅服务端
    result_filename: str | None = Field(default=None)
    result_size_bytes: int | None = Field(default=None)
    quality_notice: str | None = Field(default=None)
    error_code: str | None = Field(default=None)
    error_message: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    finished_at: datetime | None = Field(default=None)
