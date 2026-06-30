def test_upload_ok(client) -> None:
    r = client.post("/api/files", files={"file": ("demo.txt", b"hello world", "text/plain")})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["source_ext"] == ".txt"
    assert ".docx" in d["allowed_targets"]
    assert d["file_id"].startswith("file_")


def test_unsupported_type(client) -> None:
    r = client.post("/api/files", files={"file": ("hack.exe", b"x", "application/octet-stream")})
    assert r.json()["error"]["code"] == "UNSUPPORTED_FILE_TYPE"


def test_empty_file(client) -> None:
    r = client.post("/api/files", files={"file": ("empty.txt", b"", "text/plain")})
    assert r.json()["error"]["code"] == "EMPTY_FILE"


def test_too_large(client, monkeypatch) -> None:
    monkeypatch.setattr("app.routers.files.max_bytes_for", lambda ext: 5)
    r = client.post("/api/files", files={"file": ("big.txt", b"x" * 10, "text/plain")})
    assert r.json()["error"]["code"] == "FILE_TOO_LARGE"


def test_mime_mismatch_rejected(client) -> None:
    r = client.post("/api/files", files={"file": ("demo.txt", b"hello", "image/png")})
    assert r.json()["error"]["code"] == "UNSUPPORTED_FILE_TYPE"
