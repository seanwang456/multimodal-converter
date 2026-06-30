"""PDF 转换：pdf_to_txt/docx/pptx/xlsx。规格 §7.2.1 Phase 4。

- pdf_to_docx：优先用 pdf2docx 保留版式（段落/表格/字号），失败回退纯文本。
- pdf_to_xlsx：默认框线策略无表时，回退 text 定位策略提取无框线表格。
- 加密 PDF → PASSWORD_PROTECTED_PDF；pdf_to_xlsx 无表 → NO_TABLE_FOUND。
所有解析库为同步，统一走 to_thread 不阻塞事件循环。
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from app.errors import ConversionError, ErrorCode
from app.handlers.base import ConversionHandler, ConversionResult
from app.handlers.docgen import write_docx, write_pptx

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


def _pdf_to_docx_layout(input_path: str, out: Path) -> None:
    """用 pdf2docx 转 docx，尽量保留段落/表格/字号版式。失败抛异常由上层回退。"""
    from pdf2docx import Converter

    logging.getLogger("pdf2docx").setLevel(logging.WARNING)  # 抑制其 INFO 噪声
    cv = Converter(input_path)
    try:
        cv.convert(str(out), start=0, end=None)
    finally:
        cv.close()


def _consistent_table(table: list[list], min_rows: int = 2, min_cols: int = 2, consistency: float = 0.6) -> bool:
    """过滤文本策略可能产生的"伪表格"：要求行/列够多且列数一致。"""
    if not table or len(table) < min_rows:
        return False
    widths = [len(r) for r in table]
    modal = max(set(widths), key=widths.count)
    if modal < min_cols:
        return False
    return widths.count(modal) / len(widths) >= consistency


def _extract_pdf_tables(input_path: str, out: Path) -> int:
    """提取所有表格到 xlsx，返回表格数。无表抛 NO_TABLE_FOUND。

    先按默认（框线）策略；某页无表时回退 text 定位策略（提取无框线表格），并做一致性过滤。
    """
    import pdfplumber
    from openpyxl import Workbook

    TEXT_STRATEGY = {"vertical_strategy": "text", "horizontal_strategy": "text"}
    wb = Workbook()
    wb.remove(wb.active)
    count = 0
    seen: set = set()
    with pdfplumber.open(input_path) as pdf:
        for pi, page in enumerate(pdf.pages):
            tables = page.extract_tables() or []
            if not tables:
                tables = page.extract_tables(table_settings=TEXT_STRATEGY) or []
            for table in tables:
                if not _consistent_table(table):
                    continue
                sig = tuple(tuple(str(c)[:20] for c in (row or [])) for row in table[:2])
                if sig in seen:
                    continue
                seen.add(sig)
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

        # PDF → XLSX：表格提取（框线 + text 策略双保险）
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
                quality_notice="PDF 表格为结构化识别，无框线表格可能存在列错位。",
            )

        # PDF → DOCX：优先 pdf2docx 保留版式，失败回退纯文本
        if target_ext == ".docx":
            out = out_dir / "result.docx"
            try:
                await asyncio.to_thread(_pdf_to_docx_layout, input_path, out)
                return ConversionResult(
                    output_path=str(out), filename=out.name, size_bytes=out.stat().st_size,
                    quality_notice="PDF 转 Word 尽量保留原版式（段落/表格），复杂排版可能略有差异。",
                )
            except ConversionError:
                raise
            except Exception as e:
                if _is_password_error(e):
                    raise ConversionError(ErrorCode.PASSWORD_PROTECTED_PDF, "PDF 已加密，请解除密码后重新上传") from e
                # 版式转换失败 → 回退纯文本 docx（不放弃转换）
                text = await asyncio.to_thread(_extract_pdf_text_safe, input_path)
                await asyncio.to_thread(write_docx, text, out)
                return ConversionResult(
                    output_path=str(out), filename=out.name, size_bytes=out.stat().st_size,
                    quality_notice="版式还原失败，已退化为纯文本 Word。",
                )

        # txt/pptx：提取纯文本
        try:
            text = await asyncio.to_thread(_extract_pdf_text, input_path)
        except Exception as e:
            if _is_password_error(e):
                raise ConversionError(ErrorCode.PASSWORD_PROTECTED_PDF, "PDF 已加密，请解除密码后重新上传") from e
            raise ConversionError(ErrorCode.CONVERSION_ENGINE_ERROR, "PDF 文本提取失败") from e

        out = out_dir / f"result{target_ext}"
        if target_ext == ".txt":
            await asyncio.to_thread(out.write_text, text, "utf-8")
        elif target_ext == ".pptx":
            await asyncio.to_thread(write_pptx, text, out)
        return ConversionResult(
            output_path=str(out), filename=out.name, size_bytes=out.stat().st_size,
            quality_notice="PDF 转文档为 best-effort，复杂排版可能无法完全还原。",
        )


def _extract_pdf_text_safe(input_path: str) -> str:
    """docx 回退路径用的文本提取；任何异常都吞掉返回已有文本（外层已 preflight 加密）。"""
    try:
        return _extract_pdf_text(input_path)
    except Exception:  # noqa: BLE001
        return ""


PDF_HANDLERS: dict[str, ConversionHandler] = {
    k: PdfHandler(k) for k in ("pdf_to_txt", "pdf_to_docx", "pdf_to_pptx", "pdf_to_xlsx")
}
