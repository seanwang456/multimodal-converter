"""txt → docx / pdf / pptx / xlsx 真实转换（Phase 2 P0），复用 docgen，生成走 to_thread 不阻塞事件循环。"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.errors import ConversionError, ErrorCode
from app.handlers.base import ConversionHandler, ConversionResult
from app.handlers.docgen import write_docx, write_pdf, write_pptx, write_xlsx

TXT_TARGETS = {".docx", ".pdf", ".pptx", ".xlsx"}
_DISPATCH = {".docx": write_docx, ".pdf": write_pdf, ".pptx": write_pptx, ".xlsx": write_xlsx}


class TxtHandler(ConversionHandler):
    def __init__(self, key: str) -> None:
        self.key = key

    def supports(self, source_ext: str, target_ext: str) -> bool:
        return source_ext == ".txt" and target_ext in TXT_TARGETS

    async def run(
        self, input_path: str, output_dir: str, target_ext: str, options: dict[str, Any]
    ) -> ConversionResult:
        writer = _DISPATCH.get(target_ext)
        if writer is None:
            raise ConversionError(ErrorCode.UNSUPPORTED_CONVERSION, f"不支持 txt 转 {target_ext}")
        text = await asyncio.to_thread(
            lambda: Path(input_path).read_text(encoding="utf-8", errors="replace")
        )
        dest = Path(output_dir) / f"result{target_ext}"
        await asyncio.to_thread(writer, text, dest)
        return ConversionResult(
            output_path=str(dest), filename=dest.name, size_bytes=dest.stat().st_size, quality_notice=None
        )


TXT_HANDLERS: dict[str, ConversionHandler] = {
    k: TxtHandler(k) for k in ("txt_to_docx", "txt_to_pdf", "txt_to_pptx", "txt_to_xlsx")
}
