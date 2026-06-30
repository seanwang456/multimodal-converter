def test_supported_known(client) -> None:
    r = client.get("/api/conversions/supported", params={"source_ext": ".txt"})
    assert r.status_code == 200
    d = r.json()
    assert d["source_ext"] == ".txt"
    assert any(t["target_ext"] == ".docx" for t in d["targets"])


def test_supported_unknown_returns_empty(client) -> None:
    r = client.get("/api/conversions/supported", params={"source_ext": ".exe"})
    assert r.json()["targets"] == []
