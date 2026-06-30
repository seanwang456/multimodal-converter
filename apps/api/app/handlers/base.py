"""Handler 统一抽象。规格 §12。每个 handler 实现 key/supports/async run。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, TypedDict


class ConversionResult(TypedDict):
    output_path: str  # workdir 内的输出绝对路径
    filename: str
    size_bytes: int
    quality_notice: str | None


class ConversionHandler(ABC):
    key: str

    @abstractmethod
    def supports(self, source_ext: str, target_ext: str) -> bool:
        """是否支持该源→目标转换。"""

    @abstractmethod
    async def run(
        self,
        input_path: str,
        output_dir: str,
        target_ext: str,
        options: dict[str, Any],
    ) -> ConversionResult:
        """执行转换，输出写入 output_dir，返回结果元信息。"""
