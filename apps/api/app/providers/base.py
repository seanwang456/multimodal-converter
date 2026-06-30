"""OCR / ASR Provider 抽象接口。规格 §14/§15。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TypedDict


class OCRResult(TypedDict):
    text: str
    tables: list  # [{"rows": [[...], ...]}]
    confidence: float


class ASRSegment(TypedDict):
    start: float
    end: float
    text: str


class ASRResult(TypedDict):
    text: str
    segments: list[ASRSegment]
    language: str
    duration_seconds: float


class OCRProvider(ABC):
    @abstractmethod
    async def extract_text(
        self,
        image_path: str,
        language: str = "auto",
        detect_tables: bool = True,
        preserve_layout: bool = False,
    ) -> OCRResult:
        ...


class ASRProvider(ABC):
    @abstractmethod
    async def transcribe(
        self,
        audio_path: str,
        language: str = "auto",
        timestamps: bool = True,
        speaker_labels: bool = False,
    ) -> ASRResult:
        ...
