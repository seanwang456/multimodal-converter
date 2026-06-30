"""多模态 handler：图片 OCR、音频/视频 ASR → txt/docx/pdf。规格 §7.1。

provider 经 app.providers 工厂注入（默认 OpenAI-compatible；测试可覆盖 _ocr/_asr）。
阻塞操作（ffmpeg/文档生成）走异步子进程或 to_thread，不阻塞事件循环。
真实云调用需 LLM_*；handler 逻辑（抽取→生成文档）独立可测。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.config import settings
from app.errors import ConversionError, ErrorCode
from app.handlers._subprocess import run_subprocess
from app.handlers.base import ConversionHandler, ConversionResult
from app.handlers.docgen import write_docx, write_pdf
from app.providers import get_asr_provider, get_ocr_provider

IMG_SRCS = {".jpg", ".jpeg", ".png", ".bmp"}
AUDIO_SRCS = {".mp3", ".wav", ".m4a", ".aac"}
DOC_TARGETS = {".txt", ".docx", ".pdf"}


def _emit(text: str, target_ext: str, output_dir: Path) -> Path:
    out = output_dir / f"result{target_ext}"
    if target_ext == ".txt":
        out.write_text(text, encoding="utf-8")
    elif target_ext == ".docx":
        write_docx(text, out)
    elif target_ext == ".pdf":
        write_pdf(text, out)
    return out


async def _emit_async(text: str, target_ext: str, output_dir: Path) -> Path:
    return await asyncio.to_thread(_emit, text, target_ext, output_dir)


def _fmt_ts(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    return f"[{m:02d}:{s:02d}]"


def _transcript(result: dict, options: dict[str, Any]) -> str:
    segs = result.get("segments") or []
    want_ts = options.get("timestamps", True)
    if segs and want_ts:
        body = "\n".join(f"{_fmt_ts(s['start'])} {s.get('text', '').strip()}".strip() for s in segs)
        return body or result.get("text", "")
    return result.get("text", "")


async def _has_audio(input_path: str) -> bool | None:
    """ffprobe 判定是否含音频流；ffprobe 不可用时返回 None（回退到尝试提取）。"""
    try:
        _rc, out, _err = await run_subprocess(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", input_path],
            30,
        )
    except FileNotFoundError:
        return None
    except asyncio.TimeoutError:
        return None
    return bool(out.strip())


async def _extract_audio(input_path: str, audio_out: Path) -> None:
    """ffmpeg 抽音轨为 16k mono wav；失败抛 CONVERSION_ENGINE_ERROR（无音轨由上层 _has_audio 判定）。"""
    cmd = [
        settings.ffmpeg_bin, "-y", "-i", input_path, "-vn",
        "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", str(audio_out),
    ]
    try:
        rc, _out, _err = await run_subprocess(cmd, 120)
    except asyncio.TimeoutError as e:
        raise ConversionError(ErrorCode.CONVERSION_TIMEOUT, "音频提取超时") from e
    except FileNotFoundError as e:
        raise ConversionError(ErrorCode.CONVERSION_ENGINE_ERROR, "ffmpeg 未安装") from e
    if rc != 0 or not audio_out.exists():
        raise ConversionError(ErrorCode.CONVERSION_ENGINE_ERROR, "音频提取失败")


class ImageOcrHandler(ConversionHandler):
    def __init__(self, key: str) -> None:
        self.key = key

    def supports(self, source_ext: str, target_ext: str) -> bool:
        return source_ext in IMG_SRCS and target_ext in DOC_TARGETS

    async def run(self, input_path: str, output_dir: str, target_ext: str, options: dict[str, Any]) -> ConversionResult:
        provider = get_ocr_provider()
        res = await provider.extract_text(
            input_path,
            language=options.get("ocr_language", "auto"),
            detect_tables=options.get("detect_tables", True),
            preserve_layout=options.get("preserve_layout", False),
        )
        out = await _emit_async(res.get("text", ""), target_ext, Path(output_dir))
        return ConversionResult(
            output_path=str(out), filename=out.name, size_bytes=out.stat().st_size,
            quality_notice="图片 OCR 识别结果，复杂版面/手写可能存在误差。",
        )


class AudioAsrHandler(ConversionHandler):
    def __init__(self, key: str) -> None:
        self.key = key

    def supports(self, source_ext: str, target_ext: str) -> bool:
        return source_ext in AUDIO_SRCS and target_ext in DOC_TARGETS

    async def run(self, input_path: str, output_dir: str, target_ext: str, options: dict[str, Any]) -> ConversionResult:
        provider = get_asr_provider()
        res = await provider.transcribe(
            input_path,
            language=options.get("asr_language", "auto"),
            timestamps=options.get("timestamps", True),
            speaker_labels=options.get("speaker_labels", False),
        )
        text = _transcript(res, options)
        out = await _emit_async(text, target_ext, Path(output_dir))
        return ConversionResult(
            output_path=str(out), filename=out.name, size_bytes=out.stat().st_size,
            quality_notice="音频转写结果，MVP 不做精确说话人分离。",
        )


class VideoAsrHandler(ConversionHandler):
    def __init__(self, key: str) -> None:
        self.key = key

    def supports(self, source_ext: str, target_ext: str) -> bool:
        return source_ext == ".mp4" and target_ext in DOC_TARGETS

    async def run(self, input_path: str, output_dir: str, target_ext: str, options: dict[str, Any]) -> ConversionResult:
        out_dir = Path(output_dir)
        audio = out_dir / "audio.wav"
        has = await _has_audio(input_path)
        if has is False:
            raise ConversionError(ErrorCode.NO_AUDIO_TRACK, "视频中未检测到音频轨道")
        await _extract_audio(input_path, audio)
        provider = get_asr_provider()
        res = await provider.transcribe(
            str(audio),
            language=options.get("asr_language", "auto"),
            timestamps=options.get("timestamps", True),
            speaker_labels=options.get("speaker_labels", False),
        )
        text = _transcript(res, options)
        out = await _emit_async(text, target_ext, out_dir)
        return ConversionResult(
            output_path=str(out), filename=out.name, size_bytes=out.stat().st_size,
            quality_notice="视频转文本仅识别音频轨道，不包含画面 OCR。",
        )


MULTIMODAL_HANDLERS: dict[str, ConversionHandler] = {}
for _k in ("image_ocr_to_txt", "image_ocr_to_docx", "image_ocr_to_pdf"):
    MULTIMODAL_HANDLERS[_k] = ImageOcrHandler(_k)
for _k in ("audio_asr_to_txt", "audio_asr_to_docx", "audio_asr_to_pdf"):
    MULTIMODAL_HANDLERS[_k] = AudioAsrHandler(_k)
for _k in ("video_asr_to_txt", "video_asr_to_docx", "video_asr_to_pdf"):
    MULTIMODAL_HANDLERS[_k] = VideoAsrHandler(_k)
