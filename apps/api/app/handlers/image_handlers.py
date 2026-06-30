"""图片格式互转（jpg/jpeg/png/bmp），Pillow；转换走 to_thread 不阻塞事件循环。规格 §7.2.2。"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.errors import ConversionError, ErrorCode
from app.handlers.base import ConversionHandler, ConversionResult

IMG_SRCS = {".jpg", ".jpeg", ".png", ".bmp"}
IMG_TARGETS = {".jpg", ".jpeg", ".png", ".bmp"}
_FMT = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".bmp": "BMP"}


def _convert(input_path: str, target_ext: str, out: Path) -> None:
    from PIL import Image

    img = Image.open(input_path)
    fmt = _FMT.get(target_ext)
    if fmt is None:
        raise ConversionError(ErrorCode.UNSUPPORTED_CONVERSION, f"不支持图片转 {target_ext}")
    # JPEG 无透明通道：RGBA/P 转 RGB 白底
    if fmt == "JPEG" and img.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        rgba = img.convert("RGBA")
        bg.paste(rgba, mask=rgba.split()[-1])
        img = bg
    img.save(str(out), format=fmt)


class ImageFormatConvertHandler(ConversionHandler):
    key = "image_format_convert"

    def supports(self, source_ext: str, target_ext: str) -> bool:
        return source_ext in IMG_SRCS and target_ext in IMG_TARGETS and source_ext != target_ext

    async def run(
        self, input_path: str, output_dir: str, target_ext: str, options: dict[str, Any]
    ) -> ConversionResult:
        out = Path(output_dir) / f"result{target_ext}"
        await asyncio.to_thread(_convert, input_path, target_ext, out)
        return ConversionResult(
            output_path=str(out), filename=out.name, size_bytes=out.stat().st_size, quality_notice=None
        )


IMAGE_HANDLERS: dict[str, ConversionHandler] = {"image_format_convert": ImageFormatConvertHandler()}
