"""火山豆包录音文件识别 provider 单元测试。

不触网：用 FakeClient 模拟 httpx 的 submit/query 顺序响应，覆盖成功/轮询/静音/错误各分支；
格式归一化用真实 ffmpeg（不可用时 skip）；签名下载端点用 FastAPI TestClient 验证。
"""
from __future__ import annotations

import asyncio
import dataclasses
import shutil
import time

import httpx
import pytest

from app.config import settings
from app.errors import ConversionError, ErrorCode
from app.providers import volcano_asr as v
from app.providers.volcano_asr import VolcanoBigModelASRProvider, sign_source_token, verify_source_token


# ---------------- fake httpx ----------------

class FakeResp:
    def __init__(self, code="20000000", message="OK", body=None):
        self.headers = {"X-Api-Status-Code": code, "X-Api-Message": message}
        self._body = body or {}

    def json(self):
        return self._body


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.posts: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None, **kw):
        self.posts.append({"url": url, "json": json, "headers": headers})
        return self.responses.pop(0)


def _patch_httpx(monkeypatch, responses):
    fake = FakeClient(responses)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: fake)
    return fake


def _vol_settings(tmp_path, **over):
    base = dict(
        volcano_asr_api_key="key-test",
        public_base_url="https://convert.example.com",
        storage_root=tmp_path,
        asr_query_timeout_seconds=10,
        asr_query_interval_seconds=0,
    )
    base.update(over)
    return dataclasses.replace(settings, **base)


def _wav(p):
    import subprocess
    subprocess.run(
        [settings.ffmpeg_bin, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", str(p)],
        check=True, capture_output=True,
    )
    return p


# ---------------- token ----------------

def test_token_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setattr(v, "settings", _vol_settings(tmp_path))
    tok = sign_source_token("uploads/abc.mp3")
    rel, ok = verify_source_token(tok)
    assert ok and rel == "uploads/abc.mp3"


def test_token_tamper_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(v, "settings", _vol_settings(tmp_path))
    tok = sign_source_token("uploads/abc.mp3")
    exp, payload, _sig = tok.split(".")
    bad = f"{exp}.{payload}.deadbeef"
    assert verify_source_token(bad) == ("", False)
    assert verify_source_token("garbage") == ("", False)


def test_token_expired(monkeypatch, tmp_path):
    monkeypatch.setattr(v, "settings", _vol_settings(tmp_path))
    tok = sign_source_token("uploads/abc.mp3", ttl=-10)
    assert verify_source_token(tok) == ("", False)


# ---------------- _is_local ----------------

def test_is_local():
    assert v.is_local_url("http://localhost:8000")
    assert v.is_local_url("http://127.0.0.1:8000")
    assert not v.is_local_url("https://convert.example.com")


# ---------------- _parse ----------------

def test_parse_result():
    prov = VolcanoBigModelASRProvider()
    body = {
        "result": {
            "text": "你好世界。",
            "utterances": [
                {"text": "你好", "start_time": 0, "end_time": 1000},
                {"text": "世界。", "start_time": 1100, "end_time": 2000},
            ],
        },
        "audio_info": {"duration": 2000},
    }
    text, segs, dur = prov._parse(FakeResp(body=body))
    assert text == "你好世界。"
    assert segs[0] == {"start": 0.0, "end": 1.0, "text": "你好"}
    assert dur == 2.0


# ---------------- transcribe 主流程 ----------------

def test_transcribe_success(monkeypatch, tmp_path):
    monkeypatch.setattr(v, "settings", _vol_settings(tmp_path))
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    src = uploads / "x.wav"
    src.write_bytes(b"RIFF....")  # _ensure_accepted 按扩展名直接用，内容不校验

    done_body = {
        "result": {"text": "识别结果 你好", "utterances": [
            {"text": "识别结果", "start_time": 0, "end_time": 500},
            {"text": "你好", "start_time": 600, "end_time": 900},
        ]},
        "audio_info": {"duration": 900},
    }
    fake = _patch_httpx(
        monkeypatch,
        [
            FakeResp(code="20000000", message="OK"),                   # submit
            FakeResp(code="20000001", message="processing"),           # poll: 处理中
            FakeResp(code="20000002", message="queued"),               # poll: 排队中
            FakeResp(code="20000000", body=done_body),                 # poll: 完成
        ],
    )

    res = asyncio.run(VolcanoBigModelASRProvider().transcribe(str(src)))
    assert res["text"] == "识别结果 你好"
    assert len(res["segments"]) == 2 and res["segments"][1]["start"] == 0.6

    # 提交体校验：format=wav，url 为签名下载链接，header 带 X-Api-Key + Sequence
    submit = fake.posts[0]
    assert submit["json"]["audio"]["format"] == "wav"
    assert submit["json"]["audio"]["url"].startswith("https://convert.example.com/api/asr-source/")
    assert submit["json"]["request"]["model_name"] == "bigmodel"
    assert submit["headers"]["X-Api-Key"] == "key-test"
    assert submit["headers"]["X-Api-Sequence"] == "-1"
    # 查询不带 Sequence
    assert "X-Api-Sequence" not in fake.posts[1]["headers"]


def test_transcribe_silence_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(v, "settings", _vol_settings(tmp_path))
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    src = uploads / "x.wav"
    src.write_bytes(b"x")
    _patch_httpx(
        monkeypatch,
        [FakeResp(code="20000000"), FakeResp(code="20000003", message="silence")],
    )
    res = asyncio.run(VolcanoBigModelASRProvider().transcribe(str(src)))
    assert res["text"] == "" and res["segments"] == []


def test_transcribe_submit_error(monkeypatch, tmp_path):
    monkeypatch.setattr(v, "settings", _vol_settings(tmp_path))
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    src = uploads / "x.mp3"
    src.write_bytes(b"x")
    _patch_httpx(monkeypatch, [FakeResp(code="45000001", message="invalid param")])
    with pytest.raises(ConversionError) as ei:
        asyncio.run(VolcanoBigModelASRProvider().transcribe(str(src)))
    assert ei.value.code == ErrorCode.ASR_FAILED


def test_transcribe_missing_key(monkeypatch, tmp_path):
    monkeypatch.setattr(v, "settings", _vol_settings(tmp_path, volcano_asr_api_key=""))
    (tmp_path / "uploads").mkdir()
    src = tmp_path / "uploads" / "x.mp3"
    src.write_bytes(b"x")
    with pytest.raises(ConversionError) as ei:
        asyncio.run(VolcanoBigModelASRProvider().transcribe(str(src)))
    assert ei.value.code == ErrorCode.ASR_FAILED


def test_transcribe_local_base_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(v, "settings", _vol_settings(tmp_path, public_base_url="http://localhost:8000"))
    (tmp_path / "uploads").mkdir()
    src = tmp_path / "uploads" / "x.mp3"
    src.write_bytes(b"x")
    with pytest.raises(ConversionError) as ei:
        asyncio.run(VolcanoBigModelASRProvider().transcribe(str(src)))
    assert ei.value.code == ErrorCode.ASR_FAILED


# ---------------- 格式归一化 ----------------

@pytest.mark.skipif(shutil.which(settings.ffmpeg_bin) is None, reason="ffmpeg 未安装")
def test_ensure_accepted_wav_direct(tmp_path):
    p = _wav(tmp_path / "s.wav")
    path, fmt, staged = asyncio.run(v._ensure_accepted(str(p)))
    assert fmt == "wav" and staged is False and path == p


@pytest.mark.skipif(shutil.which(settings.ffmpeg_bin) is None, reason="ffmpeg 未安装")
def test_ensure_accepted_m4a_transcode(monkeypatch, tmp_path):
    monkeypatch.setattr(v, "settings", _vol_settings(tmp_path))
    import subprocess
    src = tmp_path / "s.m4a"
    subprocess.run(
        [settings.ffmpeg_bin, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-c:a", "aac", "-t", "1", str(src)],
        check=True, capture_output=True,
    )
    path, fmt, staged = asyncio.run(v._ensure_accepted(str(src)))
    try:
        assert fmt == "mp3" and staged is True
        assert path.exists() and path.suffix == ".mp3"
        assert "asr_tmp" in path.parts
    finally:
        path.unlink(missing_ok=True)


# ---------------- 签名下载端点 ----------------

def test_asr_source_endpoint_serves(client):
    # 在 conftest 的 storage_root 下放一个音频，签名后下载
    from app.services.storage import _root
    uploads = _root() / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    audio = uploads / "tok.wav"
    audio.write_bytes(b"audio-bytes")
    token = sign_source_token("uploads/tok.wav")
    r = client.get(f"/api/asr-source/{token}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("audio/wav")
    assert r.content == b"audio-bytes"


def test_asr_source_endpoint_bad_token(client):
    r = client.get("/api/asr-source/garbage.token.x")
    assert r.status_code == 410  # FILE_EXPIRED


def test_asr_source_endpoint_traversal_rejected(client):
    # 签名一个越界路径（手动用同 secret 构造）应被 _safe_join 拦截
    from app.providers.asr_source import _b64url, _secret
    import hmac, hashlib, base64
    payload = _b64url(b"../../etc/passwd")
    exp = str(int(time.time()) + 60)
    sig = base64.urlsafe_b64encode(
        hmac.new(_secret().encode(), f"{exp}.{payload}".encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    r = client.get(f"/api/asr-source/{exp}.{payload}.{sig}")
    assert r.status_code in (400, 500)  # CONVERSION_ENGINE_ERROR / 兜底


# ---------------- 工厂切换 ----------------

def test_factory_selects_volcano(monkeypatch):
    import app.providers as prov_mod
    monkeypatch.setattr(prov_mod, "settings", dataclasses.replace(settings, asr_provider="volcano"))
    prov_mod._asr = None
    try:
        assert isinstance(prov_mod.get_asr_provider(), VolcanoBigModelASRProvider)
    finally:
        prov_mod._asr = None
