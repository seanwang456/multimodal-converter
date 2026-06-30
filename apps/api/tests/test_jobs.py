from datetime import datetime, timezone

from sqlmodel import Session

from app.database import engine
from app.models import ConversionJob, new_job_id


def _upload(client, name="demo.txt", data=b"hello world") -> str:
    r = client.post("/api/files", files={"file": (name, data, "text/plain")})
    assert r.status_code == 200, r.text
    return r.json()["file_id"]


def test_full_link_upload_job_succeed_download(client) -> None:
    file_id = _upload(client)
    r = client.post("/api/jobs", json={"file_id": file_id, "target_ext": ".docx"})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]

    g = client.get(f"/api/jobs/{job_id}").json()
    assert g["status"] == "succeeded"
    assert g["progress"] == 100
    assert g["result"] is not None
    assert g["result"]["filename"].endswith(".docx")
    assert g["result"]["size_bytes"] > 0

    d = client.get(f"/api/jobs/{job_id}/download")
    assert d.status_code == 200
    assert len(d.content) > 0


def test_unsupported_conversion_rejected(client) -> None:
    file_id = _upload(client)
    r = client.post("/api/jobs", json={"file_id": file_id, "target_ext": ".mp4"})
    assert r.json()["error"]["code"] == "UNSUPPORTED_CONVERSION"


def test_frontend_handler_key_ignored(client) -> None:
    """后端为唯一校验源：前端传 handler_key 应被忽略且不影响结果。"""
    file_id = _upload(client)
    r = client.post(
        "/api/jobs",
        json={"file_id": file_id, "target_ext": ".pdf", "handler_key": "_attacker_fake"},
    )
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    g = client.get(f"/api/jobs/{job_id}").json()
    assert g["status"] == "succeeded"


def test_download_not_ready(client) -> None:
    file_id = _upload(client)
    with Session(engine) as s:
        job = ConversionJob(
            id=new_job_id(),
            file_id=file_id,
            source_ext=".txt",
            target_ext=".docx",
            handler_key="txt_to_docx",
            status="queued",
        )
        s.add(job)
        s.commit()
        jid = job.id
    d = client.get(f"/api/jobs/{jid}/download")
    assert d.json()["error"]["code"] == "DOWNLOAD_NOT_READY"
