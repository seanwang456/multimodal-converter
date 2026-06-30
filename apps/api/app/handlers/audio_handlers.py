"""音频格式互转（mp3/wav/m4a/aac），ffmpeg 异步子进程。规格 §7.2.3。"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.config import settings
from app.errors import ConversionError, ErrorCode
from app.handlers._subprocess import run_subprocess
from app.handlers.base import ConversionHandler, ConversionResult

# 规格 §7.2.3 矩阵：源 → 可转目标
AUDIO_MATRIX: dict[str, set[str]] = {
    ".mp3": {".wav"},
    ".wav": {".mp3"},
    ".m4a": {".mp3", ".wav"},
    ".aac": {".mp3", ".wav"},
}


class AudioFormatConvertHandler(ConversionHandler):
    key = "audio_format_convert"

    def supports(self, source_ext: str, target_ext: str) -> bool:
        return target_ext in AUDIO_MATRIX.get(source_ext, set())

    async def run(
        self, input_path: str, output_dir: str, target_ext: str, options: dict[str, Any]
    ) -> ConversionResult:
        src_ext = Path(input_path).suffix.lower()
        if target_ext not in AUDIO_MATRIX.get(src_ext, set()):
            raise ConversionError(ErrorCode.UNSUPPORTED_CONVERSION, "不支持该音频转换")
        out = Path(output_dir) / f"result{target_ext}"
        cmd = [settings.ffmpeg_bin, "-y", "-i", input_path, str(out)]
        try:
            rc, _out, _err = await run_subprocess(cmd, 300)
        except asyncio.TimeoutError as e:
            raise ConversionError(ErrorCode.CONVERSION_TIMEOUT, "音频转换超时") from e
        except FileNotFoundError as e:
            raise ConversionError(ErrorCode.CONVERSION_ENGINE_ERROR, "ffmpeg 未安装") from e
        if rc != 0:
            raise ConversionError(ErrorCode.CONVERSION_ENGINE_ERROR, "音频转换失败")
        return ConversionResult(
            output_path=str(out), filename=out.name, size_bytes=out.stat().st_size, quality_notice=None
        )


AUDIO_HANDLERS: dict[str, ConversionHandler] = {"audio_format_convert": AudioFormatConvertHandler()}
