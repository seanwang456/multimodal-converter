"""阿里云 filetrans ASR provider 单元测试（mock httpx，不触网）。"""
from __future__ import annotations

import asyncio
import dataclasses

import httpx
import pytest

from app.config import settings
from app.errors import ConversionError, ErrorCode
from app.providers import aliyun_asr as a
from app.providers.aliyun_asr import AliyunFiletransASRProvider


class FakeResp:
    def __init__(self, json_data=None, text=""):
        self._json = json_data or {}
        self.text = text
        self.status_code = 200

    def json(self):
        return self._json


class FakeClient:
    def __init__(self, post_responses, get_responses):
        self._post = list(post_responses)
        self._get = list(get_responses)
        self.posts: list[str] = []
        self.gets: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kw):
        self.posts.append(url)
        return self._post.pop(0)

    async def get(self, url, **kw):
        self.gets.append(url)
        return self._get.pop(0)


def _patch(monkeypatch, post_responses, get_responses):
    fake = FakeClient(post_responses, get_responses)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: fake)
    return fake


def _cfg(tmp_path, **over):
    base = dict(
        llm_base_url="https://ws-test.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        llm_api_key="sk-test",
        aliyun_asr_base_url="",
        aliyun_asr_api_key="",
        aliyun_asr_model="qwen3-asr-flash-filetrans",
        public_base_url="https://convert.example.com",
        storage_root=tmp_path,
        asr_query_timeout_seconds=10,
        asr_query_interval_seconds=0,
    )
    base.update(over)
    return dataclasses.replace(settings, **base)


def _wav(tmp_path):
    up = tmp_path / "uploads"
    up.mkdir()
    p = up / "x.wav"
    p.write_bytes(b"RIFF....")
    return p


RESULT_JSON = {
    "transcripts": [
        {
            "channel_id": 0,
            "text": "你好世界。",
            "sentences": [
                {"begin_time": 0, "end_time": 1000, "text": "你好"},
                {"begin_time": 1100, "end_time": 2000, "text": "世界。"},
            ],
        }
    ]
}


def test_transcribe_success(monkeypatch, tmp_path):
    monkeypatch.setattr(a, "settings", _cfg(tmp_path))
    src = _wav(tmp_path)
    post_q = [FakeResp({"output": {"task_id": "t1", "task_status": "PENDING"}})]
    get_q = [
        FakeResp({"output": {"task_status": "RUNNING"}}),
        FakeResp({"output": {"task_status": "SUCCEEDED", "result": {"transcription_url": "https://res/x.json"}}, "usage": {"seconds": 2}}),
        FakeResp(RESULT_JSON),
    ]
    fake = _patch(monkeypatch, post_q, get_q)

    res = asyncio.run(AliyunFiletransASRProvider().transcribe(str(src)))
    assert res["text"] == "你好世界。"
    assert len(res["segments"]) == 2 and res["segments"][1]["start"] == 1.1
    assert res["duration_seconds"] == 2.0

    # 提交走 /api/v1/services/audio/asr/transcription，且 body 含 file_url 与 X-DashScope-Async
    assert fake.posts[0].endswith("/api/v1/services/audio/asr/transcription")
    # 轮询走 /api/v1/tasks/{id}，最后下载 transcription_url
    assert any("/api/v1/tasks/t1" in u for u in fake.gets)
    assert fake.gets[-1] == "https://res/x.json"


def test_submit_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(a, "settings", _cfg(tmp_path))
    src = _wav(tmp_path)
    post_q = [FakeResp({"code": "Model.AccessDenied", "message": "denied"})]
    _patch(monkeypatch, post_q, [])
    with pytest.raises(ConversionError) as ei:
        asyncio.run(AliyunFiletransASRProvider().transcribe(str(src)))
    assert ei.value.code == ErrorCode.ASR_FAILED


def test_poll_failed(monkeypatch, tmp_path):
    monkeypatch.setattr(a, "settings", _cfg(tmp_path))
    src = _wav(tmp_path)
    post_q = [FakeResp({"output": {"task_id": "t1", "task_status": "PENDING"}})]
    get_q = [FakeResp({"output": {"task_status": "FAILED", "code": "FILE_403_FORBIDDEN", "message": "forbidden"}})]
    _patch(monkeypatch, post_q, get_q)
    with pytest.raises(ConversionError) as ei:
        asyncio.run(AliyunFiletransASRProvider().transcribe(str(src)))
    assert ei.value.code == ErrorCode.ASR_FAILED


def test_missing_key(monkeypatch, tmp_path):
    monkeypatch.setattr(a, "settings", _cfg(tmp_path, llm_api_key="", aliyun_asr_api_key=""))
    src = _wav(tmp_path)
    with pytest.raises(ConversionError) as ei:
        asyncio.run(AliyunFiletransASRProvider().transcribe(str(src)))
    assert ei.value.code == ErrorCode.ASR_FAILED


def test_local_base_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(a, "settings", _cfg(tmp_path, public_base_url="http://localhost:8000"))
    src = _wav(tmp_path)
    with pytest.raises(ConversionError) as ei:
        asyncio.run(AliyunFiletransASRProvider().transcribe(str(src)))
    assert ei.value.code == ErrorCode.ASR_FAILED


def test_host_derived_from_llm_base(monkeypatch, tmp_path):
    monkeypatch.setattr(a, "settings", _cfg(tmp_path))
    assert a._host() == "https://ws-test.cn-beijing.maas.aliyuncs.com"
    monkeypatch.setattr(a, "settings", _cfg(tmp_path, aliyun_asr_base_url="https://other.aliyuncs.com"))
    assert a._host() == "https://other.aliyuncs.com"


def test_factory_selects_aliyun(monkeypatch):
    import app.providers as prov_mod
    monkeypatch.setattr(prov_mod, "settings", dataclasses.replace(settings, asr_provider="aliyun"))
    prov_mod._asr = None
    try:
        assert isinstance(prov_mod.get_asr_provider(), AliyunFiletransASRProvider)
    finally:
        prov_mod._asr = None
