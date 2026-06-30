"""测试公共夹具：重定向存储/DB 到临时目录，强制 Inline runner（无需 Redis）。"""
from __future__ import annotations

import os
import pathlib
import tempfile

_tmp = tempfile.mkdtemp(prefix="conv_test_")
os.environ["STORAGE_ROOT"] = _tmp
os.environ["SQLITE_PATH"] = str(pathlib.Path(_tmp) / "test.db")
os.environ["REDIS_URL"] = "redis://invalid.local:1/0"  # 强制 Inline 回退
os.environ["CORS_ORIGINS"] = "http://localhost:3000"

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, delete

from app import main as app_main
from app.database import engine, init_db
from app.models import ConversionJob, FileRecord
from app.services import job_runner
from app.services.job_runner import InlineJobRunner


@pytest.fixture()
def client():
    init_db()
    with Session(engine) as s:
        s.exec(delete(ConversionJob))
        s.exec(delete(FileRecord))
        s.commit()
    job_runner._runner = InlineJobRunner()
    with TestClient(app_main.app) as c:
        yield c
