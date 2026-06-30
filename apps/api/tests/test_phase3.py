"""Phase 3 多模态 handler 测试：注入 fake provider 验证抽取→生成文档链路，ffmpeg 抽音轨为真。"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from app.config import settings
from app.handlers.multimodal_handlers import MULTIMODAL_HANDLERS
from app.providers import base as providers_pkg  # noqa: F401
from app.providers import get_asr_provider, get_ocr_provider
from app.providers.base import ASRProvider, OCRProvider
from app import providers as prov_mod


class FakeOCR(OCRProvider):
    async def extract_text(self, image_path, language="auto", detect_tables=True, preserve_layout=False):
        return {"text": "图片识别文字 你好", "tables": [], "confidence": 0.9}


class FakeASR(ASRProvider):
    async def transcribe(self, audio_path, language="auto", timestamps=True, speaker_labels=False):
        return {
            "text": "完整转写 你好世界",
            "segments": [{"start": 0.0, "end": 1.0, "text": "你好世界"}],
            "language": "zh", "duration_seconds": 1.0,
        }


@pytest.fixture()
def fake_providers():
    prov_mod._ocr = FakeOCR()
    prov_mod._asr = FakeASR()
    yield
    prov_mod._ocr = None
    prov_mod._asr = None


def _run(handler, input_path: Path, out_dir: Path, ext: str, options=None) -> dict:
    return asyncio.run(handler.run(str(input_path), str(out_dir), ext, options or {}))


def _make_png(p: Path) -> Path:
    from PIL import Image
    Image.new("RGB", (16, 16), (1, 2, 3)).save(str(p))
    return p


def _make_wav(p: Path) -> Path:
    subprocess.run(
        [settings.ffmpeg_bin, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", str(p)],
        check=True, capture_output=True,
    )
    return p


def _make_mp4(p: Path, with_audio: bool = True) -> Path:
    cmd = [settings.ffmpeg_bin, "-y", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=1", "-t", "1"]
    if with_audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-shortest"]
    else:
        cmd += ["-an"]
    cmd.append(str(p))
    subprocess.run(cmd, check=True, capture_output=True)
    return p


# ---------- image OCR ----------
def test_image_ocr_to_txt(tmp_path, fake_providers) -> None:
    src = _make_png(tmp_path / "s.png")
    out_dir = tmp_path / "out"; out_dir.mkdir()
    res = _run(MULTIMODAL_HANDLERS["image_ocr_to_txt"], src, out_dir, ".txt")
    out = Path(res["output_path"])
    assert "图片识别文字" in out.read_text(encoding="utf-8")


def test_image_ocr_to_pdf(tmp_path, fake_providers) -> None:
    src = _make_png(tmp_path / "s.png")
    out_dir = tmp_path / "out"; out_dir.mkdir()
    res = _run(MULTIMODAL_HANDLERS["image_ocr_to_pdf"], src, out_dir, ".pdf")
    assert Path(res["output_path"]).read_bytes()[:4] == b"%PDF"


# ---------- audio ASR ----------
@pytest.mark.skipif(shutil.which(settings.ffmpeg_bin) is None, reason="ffmpeg 未安装")
def test_audio_asr_to_txt_with_timestamp(tmp_path, fake_providers) -> None:
    src = _make_wav(tmp_path / "s.wav")
    out_dir = tmp_path / "out"; out_dir.mkdir()
    res = _run(MULTIMODAL_HANDLERS["audio_asr_to_txt"], src, out_dir, ".txt")
    content = Path(res["output_path"]).read_text(encoding="utf-8")
    assert "你好世界" in content and "[00:00]" in content


# ---------- video ASR ----------
@pytest.mark.skipif(shutil.which(settings.ffmpeg_bin) is None, reason="ffmpeg 未安装")
def test_video_asr_to_txt(tmp_path, fake_providers) -> None:
    src = _make_mp4(tmp_path / "s.mp4", with_audio=True)
    out_dir = tmp_path / "out"; out_dir.mkdir()
    res = _run(MULTIMODAL_HANDLERS["video_asr_to_txt"], src, out_dir, ".txt")
    assert "你好世界" in Path(res["output_path"]).read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which(settings.ffmpeg_bin) is None, reason="ffmpeg 未安装")
def test_video_no_audio_track(tmp_path, fake_providers) -> None:
    from app.errors import ConversionError, ErrorCode
    src = _make_mp4(tmp_path / "s.mp4", with_audio=False)
    out_dir = tmp_path / "out"; out_dir.mkdir()
    try:
        _run(MULTIMODAL_HANDLERS["video_asr_to_txt"], src, out_dir, ".txt")
        assert False, "应抛 NO_AUDIO_TRACK"
    except ConversionError as e:
        assert e.code == ErrorCode.NO_AUDIO_TRACK


# ---------- registry 矩阵 ----------
def test_registry_phase3_routes() -> None:
    from app.services import registry
    registry.register_handlers()
    jpg_targets = {r["target_ext"] for r in registry.get_targets(".jpg")}
    assert {".png", ".bmp", ".txt", ".docx", ".pdf"} <= jpg_targets
    mp4_targets = {r["target_ext"] for r in registry.get_targets(".mp4")}
    assert {".txt", ".docx", ".pdf"} <= mp4_targets


# ---------- 端到端（fake provider 经 API）----------
def test_e2e_image_ocr_to_txt(client, tmp_path, fake_providers) -> None:
    from PIL import Image
    p = tmp_path / "e.png"
    Image.new("RGB", (8, 8), (1, 1, 1)).save(str(p))
    with open(p, "rb") as f:
        r = client.post("/api/files", files={"file": ("e.png", f.read(), "image/png")})
    assert r.status_code == 200
    fid = r.json()["file_id"]
    r = client.post("/api/jobs", json={"file_id": fid, "target_ext": ".txt"})
    jid = r.json()["job_id"]
    g = client.get(f"/api/jobs/{jid}").json()
    assert g["status"] == "succeeded"
    d = client.get(f"/api/jobs/{jid}/download")
    assert d.status_code == 200 and "图片识别文字" in d.content.decode("utf-8")
