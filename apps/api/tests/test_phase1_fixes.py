"""Codex REVISE 修复的回归测试。"""
from datetime import timedelta

from sqlmodel import Session

from app.database import engine
from app.models import FileRecord, new_file_id, _now
from app.services import job_runner, storage


def _make_expired_file(data: bytes = b"old data") -> str:
    rel = storage.save_upload(data, ".txt")
    with Session(engine) as s:
        f = FileRecord(
            id=new_file_id(),
            original_filename="old.txt",
            stored_path=rel,
            source_ext=".txt",
            mime_type="text/plain",
            size_bytes=len(data),
            status="uploaded",
            created_at=_now() - timedelta(hours=48),
            expires_at=_now() - timedelta(hours=24),
        )
        s.add(f)
        s.commit()
        s.refresh(f)
        return f.id


def test_sweep_expired_marks_and_deletes(client) -> None:
    rel = storage.save_upload(b"old data", ".txt")
    with Session(engine) as s:
        f = FileRecord(
            id=new_file_id(), original_filename="old.txt", stored_path=rel,
            source_ext=".txt", mime_type="text/plain", size_bytes=8,
            status="uploaded",
            created_at=_now() - timedelta(hours=48),
            expires_at=_now() - timedelta(hours=24),
        )
        s.add(f)
        s.commit()
        s.refresh(f)
        fid = f.id
    assert storage.upload_abspath(rel).exists()

    with Session(engine) as s:
        removed = storage.sweep_expired(s)

    assert removed >= 1
    with Session(engine) as s:
        assert s.get(FileRecord, fid).status == "expired"
    assert not storage.upload_abspath(rel).exists()


def test_mime_strict_jpg_png_rejected(client) -> None:
    r = client.post("/api/files", files={"file": ("a.jpg", b"\xff\xd8\xff", "image/png")})
    assert r.json()["error"]["code"] == "UNSUPPORTED_FILE_TYPE"


def test_mime_strict_txt_ok(client) -> None:
    r = client.post("/api/files", files={"file": ("a.txt", b"hi", "text/plain")})
    assert r.status_code == 200


def test_pdf_upload_returns_phase4_targets_no_orphan(client) -> None:
    # Phase 4 后 .pdf 有 txt/docx/xlsx/pptx 转换目标；上传成功且无孤儿
    r = client.post("/api/files", files={"file": ("a.pdf", b"%PDF-1.4 test", "application/pdf")})
    assert r.status_code == 200, r.text
    targets = set(r.json()["allowed_targets"])
    assert {".txt", ".docx", ".xlsx", ".pptx"} <= targets


def test_create_job_expired_file_rejected(client) -> None:
    fid = _make_expired_file(b"x")
    r = client.post("/api/jobs", json={"file_id": fid, "target_ext": ".docx"})
    assert r.json()["error"]["code"] == "FILE_EXPIRED"


def test_runner_get_state(client) -> None:
    r = client.post("/api/files", files={"file": ("d.txt", b"hello", "text/plain")})
    fid = r.json()["file_id"]
    r = client.post("/api/jobs", json={"file_id": fid, "target_ext": ".docx"})
    jid = r.json()["job_id"]
    st = job_runner.get_runner().get_state(jid)
    assert st is not None
    assert st["status"] == "succeeded"
