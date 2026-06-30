"""PDF 转换：pdf_to_txt/docx/pptx/xlsx（pdfplumber）。规格 §7.2.1 Phase 4。

加密 PDF → PASSWORD_PROTECTED_PDF；pdf_to_xlsx 无表 → NO_TABLE_FOUND。
pdfplumber 为同步库，全部走 to_thread 不阻塞事件循环。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.errors import ConversionError, ErrorCode
from app.handlers.base import ConversionHandler, ConversionResult
from app.handlers.docgen import write_docx, write_pdf, write_pptx

PDF_TARGETS = {".txt", ".docx", ".pptx", ".xlsx"}


def _is_password_error(e: Exception) -> bool:
    msg = str(e).lower()
    return any(k in msg for k in ("password", "encrypt", "not allowed", "restricted"))


def _is_encrypted_pdf(input_path: str) -> bool:
    """显式加密 preflight（pypdf 的 is_encrypted 可靠，不依赖异常文本）。"""
    try:
        from pypdf import PdfReader

        return bool(PdfReader(input_path).is_encrypted)
    except Exception:  # noqa: BLE001
        return False


def _extract_pdf_text(input_path: str) -> str:
    import pdfplumber

    parts: list[str] = []
    with pdfplumber.open(input_path) as pdf:
        for page in pdf.pages:
            parts.append(page.extract_text() or "")
    return "\n\n".join(parts).strip()


def _extract_pdf_tables(input_path: str, out: Path) -> int:
    """提取所有表格到 xlsx，返回表格数。无表抛 NO_TABLE_FOUND。"""
    import pdfplumber
    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)
    count = 0
    with pdfplumber.open(input_path) as pdf:
        for pi, page in enumerate(pdf.pages):
            for table in page.extract_tables() or []:
                ws = wb.create_sheet(title=f"p{pi}_t{count}"[:31])
                for row in table:
                    ws.append(["" if c is None else str(c) for c in row])
                count += 1
    if count == 0:
        raise ConversionError(ErrorCode.NO_TABLE_FOUND, "未识别到可导出的表格")
    wb.save(str(out))
    return count


class PdfHandler(ConversionHandler):
    def __init__(self, key: str) -> None:
        self.key = key

    def supports(self, source_ext: str, target_ext: str) -> bool:
        return source_ext == ".pdf" and target_ext in PDF_TARGETS

    async def run(self, input_path: str, output_dir: str, target_ext: str, options: dict[str, Any]) -> ConversionResult:
        if await asyncio.to_thread(_is_encrypted_pdf, input_path):
            raise ConversionError(ErrorCode.PASSWORD_PROTECTED_PDF, "PDF 已加密，请解除密码后重新上传")
        out_dir = Path(output_dir)
        if target_ext == ".xlsx":
            out = out_dir / "result.xlsx"
            try:
                await asyncio.to_thread(_extract_pdf_tables, input_path, out)
            except ConversionError:
                raise
            except Exception as e:
                if _is_password_error(e):
                    raise ConversionError(ErrorCode.PASSWORD_PROTECTED_PDF, "PDF 已加密，请解除密码后重新上传") from e
                raise ConversionError(ErrorCode.CONVERSION_ENGINE_ERROR, "PDF 表格解析失败") from e
            return ConversionResult(
                output_path=str(out), filename=out.name, size_bytes=out.stat().st_size,
                quality_notice="PDF 表格识别仅在有明显表格时效果较好。",
            )

        # txt/docx/pptx：先提取文本
        try:
            text = await asyncio.to_thread(_extract_pdf_text, input_path)
        except Exception as e:
            if _is_password_error(e):
                raise ConversionError(ErrorCode.PASSWORD_PROTECTED_PDF, "PDF 已加密，请解除密码后重新上传") from e
            raise ConversionError(ErrorCode.CONVERSION_ENGINE_ERROR, "PDF 文本提取失败") from e

        out = out_dir / f"result{target_ext}"
        if target_ext == ".txt":
            await asyncio.to_thread(out.write_text, text, "utf-8")
        elif target_ext == ".docx":
            await asyncio.to_thread(write_docx, text, out)
        elif target_ext == ".pptx":
            await asyncio.to_thread(write_pptx, text, out)
        return ConversionResult(
            output_path=str(out), filename=out.name, size_bytes=out.stat().st_size,
            quality_notice="PDF 转文档为 best-effort，复杂排版可能无法完全还原。",
        )


PDF_HANDLERS: dict[str, ConversionHandler] = {
    k: PdfHandler(k) for k in ("pdf_to_txt", "pdf_to_docx", "pdf_to_pptx", "pdf_to_xlsx")
}
