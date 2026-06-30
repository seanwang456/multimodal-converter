"""Provider 工厂。默认 OpenAI-compatible 实现；测试可通过覆盖 _ocr/_asr 注入 fake。

ASR_PROVIDER=volcano 时切换为火山豆包录音文件识别（标准版）。
"""
from __future__ import annotations

from app.config import settings
from app.providers.base import ASRProvider, OCRProvider

_ocr: OCRProvider | None = None
_asr: ASRProvider | None = None


def get_ocr_provider() -> OCRProvider:
    global _ocr
    if _ocr is None:
        from app.providers.llm_provider import OpenAIOCRProvider

        _ocr = OpenAIOCRProvider()
    return _ocr


def get_asr_provider() -> ASRProvider:
    global _asr
    if _asr is None:
        if (settings.asr_provider or "openai").lower() == "volcano":
            from app.providers.volcano_asr import VolcanoBigModelASRProvider

            _asr = VolcanoBigModelASRProvider()
        else:
            from app.providers.llm_provider import OpenAIASRProvider

            _asr = OpenAIASRProvider()
    return _asr
