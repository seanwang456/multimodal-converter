"""OpenAI-compatible 多模态 provider 实现（OCR 走 vision，ASR 走 audio/transcriptions）。

需配置 LLM_BASE_URL / LLM_API_KEY（及可选 LLM_VISION_MODEL / LLM_ASR_MODEL）。
缺凭证按 provider 类型抛 OCR_FAILED / ASR_FAILED；调用失败同。不致 worker 崩溃。
"""
from __future__ import annotations

import base64
import io
import os
from pathlib import Path

from app.config import settings
from app.errors import ConversionError, ErrorCode
from app.providers.base import ASRProvider, OCRProvider, ASRResult, OCRResult


def _creds(err_code: str) -> tuple[str, str]:
    base = (settings.llm_base_url or os.getenv("LLM_BASE_URL") or "").rstrip("/")
    key = settings.llm_api_key or os.getenv("LLM_API_KEY") or ""
    if not base or not key:
        raise ConversionError(err_code, "未配置多模态 provider（LLM_BASE_URL/LLM_API_KEY）")
    return base, key


def _image_data_url(image_path: str) -> str:
    """读取图片并归一化为 JPEG/PNG 的 base64 data url（bmp 等格式统一转 PNG）。"""
    from PIL import Image

    img = Image.open(image_path)
    fmt = (img.format or "").upper()
    buf = io.BytesIO()
    if fmt == "JPEG":
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.save(buf, format="JPEG")
        mime = "image/jpeg"
    else:
        # PNG/BMP/其它 → 统一转 PNG（vision API 普遍支持 PNG）
        if img.mode not in ("RGB", "RGBA", "L"):
            img = img.convert("RGB")
        img.save(buf, format="PNG")
        mime = "image/png"
    return f"data:{mime};base64,{base64.b64encode(buf.getvalue()).decode()}"


class OpenAIOCRProvider(OCRProvider):
    async def extract_text(
        self, image_path: str, language: str = "auto",
        detect_tables: bool = True, preserve_layout: bool = False,
    ) -> OCRResult:
        import httpx

        base, key = _creds(ErrorCode.OCR_FAILED)
        model = os.getenv("LLM_VISION_MODEL", "gpt-4o")
        data_url = await _to_thread(_image_data_url, image_path)
        prompt = "请提取图片中的全部文字，保持原有顺序与结构。" + (
            " 如有明显表格，用 Markdown 表格输出。" if detect_tables else ""
        )
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]}],
            "max_tokens": 4096,
        }
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                r = await client.post(f"{base}/chat/completions", json=payload,
                                      headers={"Authorization": f"Bearer {key}"}, follow_redirects=True)
                r.raise_for_status()
                text = r.json()["choices"][0]["message"]["content"].strip()
        except ConversionError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ConversionError(ErrorCode.OCR_FAILED, "图片文字识别失败，请更换清晰图片") from e
        return OCRResult(text=text, tables=[], confidence=0.0)


async def _to_thread(func, *args):
    import asyncio

    return await asyncio.to_thread(func, *args)


class OpenAIASRProvider(ASRProvider):
    async def transcribe(
        self, audio_path: str, language: str = "auto",
        timestamps: bool = True, speaker_labels: bool = False,
    ) -> ASRResult:
        import httpx

        base, key = _creds(ErrorCode.ASR_FAILED)
        model = os.getenv("LLM_ASR_MODEL", "whisper-1")
        try:
            data_bytes = await _to_thread(Path(audio_path).read_bytes)
            files = {"file": (Path(audio_path).name, data_bytes)}
            data = {"model": model, "response_format": "verbose_json"}
            if language != "auto":
                data["language"] = language
            async with httpx.AsyncClient(timeout=300) as client:
                r = await client.post(f"{base}/audio/transcriptions", data=data, files=files,
                                      headers={"Authorization": f"Bearer {key}"}, follow_redirects=True)
                r.raise_for_status()
                body = r.json()
        except ConversionError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ConversionError(ErrorCode.ASR_FAILED, "音频识别失败，请检查音频质量") from e

        text = body.get("text", "").strip()
        segments = [
            {"start": s.get("start", 0.0), "end": s.get("end", 0.0), "text": s.get("text", "").strip()}
            for s in body.get("segments", [])
        ]
        return ASRResult(text=text, segments=segments,
                         language=body.get("language", language),
                         duration_seconds=body.get("duration", 0.0))
