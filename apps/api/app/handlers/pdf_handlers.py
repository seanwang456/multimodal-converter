"""PDF 转换：pdf_to_txt/docx/pptx/xlsx。规格 §7.2.1 Phase 4。

- pdf_to_docx：优先用 pdf2docx 保留版式（段落/表格/字号），失败回退纯文本。
- pdf_to_xlsx：默认框线策略无表时，回退 text 定位策略提取无框线表格。
- 加密 PDF → PASSWORD_PROTECTED_PDF；pdf_to_xlsx 无表 → NO_TABLE_FOUND。
所有解析库为同步，统一走 to_thread 不阻塞事件循环。
"""
from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import settings
from app.errors import ConversionError, ErrorCode
from app.handlers.base import ConversionHandler, ConversionResult
from app.handlers.docgen import write_docx, write_pptx
from app.providers import get_ocr_provider

PDF_TARGETS = {".txt", ".docx", ".pptx", ".xlsx"}
OCR_QUALITY_NOTICE = "扫描页面经 OCR 识别，复杂版面、手写内容或低清晰度页面可能存在误差。"
_PDFIUM_RENDER_LOCK = threading.Lock()

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PdfPageState:
    index: int
    native_text: str
    needs_ocr: bool


@dataclass(frozen=True)
class PdfTextResult:
    text: str
    ocr_pages: int


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


def _has_page_sized_image(page) -> bool:  # noqa: ANN001
    """页面存在覆盖至少一半版面的图片时，视为可能包含扫描正文。"""
    page_area = float(page.width * page.height)
    if page_area <= 0:
        return False
    for image in page.images:
        try:
            width = max(0.0, float(image.get("x1", 0)) - float(image.get("x0", 0)))
            height = max(0.0, float(image.get("bottom", 0)) - float(image.get("top", 0)))
        except (TypeError, ValueError):
            continue
        if width * height / page_area >= 0.5:
            return True
    return False


def _page_needs_ocr(page, text: str) -> bool:  # noqa: ANN001
    """无文本层，或仅有少量文字叠在整页图片上时触发 OCR。"""
    compact = "".join(text.split())
    return not compact or (len(compact) < 20 and _has_page_sized_image(page))


def _inspect_pdf_pages(input_path: str) -> list[PdfPageState]:
    """一次读取每页原生文本并判定是否需要 OCR。"""
    import pdfplumber

    states: list[PdfPageState] = []
    with pdfplumber.open(input_path) as pdf:
        for index, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            states.append(
                PdfPageState(
                    index=index,
                    native_text=text,
                    needs_ocr=_page_needs_ocr(page, text),
                )
            )
    return states


def _render_pdf_page(input_path: str, page_index: int, dest: Path) -> None:
    """将单页渲染为 OCR Provider 可读的 PNG。"""
    import pdfplumber

    with _PDFIUM_RENDER_LOCK:
        with pdfplumber.open(input_path) as pdf:
            page_image = pdf.pages[page_index].to_image(resolution=250, antialias=True)
            page_image.save(str(dest), format="PNG")


async def _render_pdf_page_for_ocr(
    input_path: str,
    page_index: int,
    dest: Path,
) -> None:
    render_task = asyncio.create_task(
        asyncio.to_thread(_render_pdf_page, input_path, page_index, dest),
    )
    try:
        await asyncio.shield(render_task)
    except asyncio.CancelledError:
        try:
            await render_task
        except Exception:  # cancellation remains the externally visible result
            pass
        raise


async def _extract_pdf_text_with_ocr(
    input_path: str,
    output_dir: Path,
    options: dict[str, Any],
    pages: list[PdfPageState] | None = None,
) -> PdfTextResult:
    """按页合并原生文本与 OCR 文本，并及时清理渲染图片。"""
    states = pages if pages is not None else await asyncio.to_thread(
        _inspect_pdf_pages, input_path,
    )
    ocr_states = [state for state in states if state.needs_ocr]
    provider = get_ocr_provider() if ocr_states else None
    parts = [
        state.native_text.strip() if not state.needs_ocr else ""
        for state in states
    ]

    if not ocr_states:
        return PdfTextResult(text="\n\n".join(parts).strip(), ocr_pages=0)
    if provider is None:
        raise ConversionError(ErrorCode.OCR_FAILED, "OCR Provider 不可用")

    worker_count = min(settings.pdf_ocr_page_concurrency, len(ocr_states))
    next_position = 0
    stop = asyncio.Event()
    errors: list[tuple[int, Exception]] = []
    log.info(
        "PDF OCR 启动：扫描页 %d，页级并发 %d",
        len(ocr_states),
        worker_count,
    )

    async def worker() -> None:
        nonlocal next_position
        while not stop.is_set() and next_position < len(ocr_states):
            state = ocr_states[next_position]
            next_position += 1
            image_path = output_dir / f"ocr-page-{state.index + 1}.png"
            try:
                try:
                    await _render_pdf_page_for_ocr(
                        input_path, state.index, image_path,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    raise ConversionError(
                        ErrorCode.CONVERSION_ENGINE_ERROR,
                        "PDF 页面渲染失败",
                    ) from exc
                result = await provider.extract_text(
                    str(image_path),
                    language=options.get("ocr_language", "auto"),
                    detect_tables=options.get("detect_tables", True),
                    preserve_layout=options.get("preserve_layout", False),
                )
                parts[state.index] = (result.get("text") or "").strip()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                errors.append((state.index, exc))
                stop.set()
            finally:
                image_path.unlink(missing_ok=True)

    async with asyncio.TaskGroup() as group:
        for _ in range(worker_count):
            group.create_task(worker())

    if errors:
        errors.sort(key=lambda item: item[0])
        raise errors[0][1]

    return PdfTextResult(
        text="\n\n".join(parts).strip(),
        ocr_pages=len(ocr_states),
    )


def _ocr_quality_notice(result: PdfTextResult) -> str:
    notice = OCR_QUALITY_NOTICE
    if not result.text:
        notice += " 未识别到有效文字。"
    return notice


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

        pages: list[PdfPageState] | None = None
        if target_ext in {".txt", ".docx", ".pptx"}:
            try:
                pages = await asyncio.to_thread(_inspect_pdf_pages, input_path)
            except Exception as e:
                if _is_password_error(e):
                    raise ConversionError(
                        ErrorCode.PASSWORD_PROTECTED_PDF,
                        "PDF 已加密，请解除密码后重新上传",
                    ) from e
                raise ConversionError(
                    ErrorCode.CONVERSION_ENGINE_ERROR, "PDF 文本提取失败",
                ) from e

        # PDF → DOCX：扫描页走 OCR 生成可编辑文字；纯文字 PDF 保留版式转换路径
        if target_ext == ".docx":
            out = out_dir / "result.docx"
            assert pages is not None
            if any(page.needs_ocr for page in pages):
                extracted = await _extract_pdf_text_with_ocr(
                    input_path, out_dir, options, pages,
                )
                await asyncio.to_thread(write_docx, extracted.text, out)
                return ConversionResult(
                    output_path=str(out), filename=out.name, size_bytes=out.stat().st_size,
                    quality_notice=_ocr_quality_notice(extracted),
                )
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
                text = "\n\n".join(page.native_text for page in pages).strip()
                await asyncio.to_thread(write_docx, text, out)
                return ConversionResult(
                    output_path=str(out), filename=out.name, size_bytes=out.stat().st_size,
                    quality_notice="版式还原失败，已退化为纯文本 Word。",
                )

        # PDF → TXT：原生文本页直接提取，扫描页按需 OCR
        if target_ext == ".txt":
            assert pages is not None
            extracted = await _extract_pdf_text_with_ocr(
                input_path, out_dir, options, pages,
            )
            out = out_dir / "result.txt"
            await asyncio.to_thread(out.write_text, extracted.text, "utf-8")
            return ConversionResult(
                output_path=str(out), filename=out.name, size_bytes=out.stat().st_size,
                quality_notice=(
                    _ocr_quality_notice(extracted)
                    if extracted.ocr_pages
                    else "PDF 转 TXT 仅保留纯文本，丢弃排版和图片。"
                ),
            )

        # PDF → PPTX：原生文本页直接提取，扫描页按需 OCR
        assert pages is not None
        extracted = await _extract_pdf_text_with_ocr(
            input_path, out_dir, options, pages,
        )
        out = out_dir / "result.pptx"
        await asyncio.to_thread(write_pptx, extracted.text, out)
        return ConversionResult(
            output_path=str(out), filename=out.name, size_bytes=out.stat().st_size,
            quality_notice=(
                _ocr_quality_notice(extracted)
                if extracted.ocr_pages
                else "PDF 转 PPT 为 best-effort，复杂排版可能无法完全还原。"
            ),
        )


PDF_HANDLERS: dict[str, ConversionHandler] = {
    k: PdfHandler(k) for k in ("pdf_to_txt", "pdf_to_docx", "pdf_to_pptx", "pdf_to_xlsx")
}
