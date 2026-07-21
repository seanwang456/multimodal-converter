"""扫描型/混合型 PDF → TXT/DOCX 的 OCR 回归测试。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from PIL import Image, ImageDraw
from docx import Document
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

import app.providers as providers
from app.errors import ConversionError, ErrorCode
from app.handlers.pdf_handlers import PDF_HANDLERS
from app.providers.base import OCRProvider, OCRResult


class RecordingOCR(OCRProvider):
    def __init__(self, text: str = "扫描页 OCR 文字") -> None:
        self.text = text
        self.calls: list[dict] = []

    async def extract_text(
        self,
        image_path: str,
        language: str = "auto",
        detect_tables: bool = True,
        preserve_layout: bool = False,
    ) -> OCRResult:
        path = Path(image_path)
        assert path.suffix == ".png"
        assert path.exists()
        self.calls.append(
            {
                "language": language,
                "detect_tables": detect_tables,
                "preserve_layout": preserve_layout,
            }
        )
        return OCRResult(text=self.text, tables=[], confidence=0.9)


class FailingOCR(OCRProvider):
    async def extract_text(
        self,
        image_path: str,
        language: str = "auto",
        detect_tables: bool = True,
        preserve_layout: bool = False,
    ) -> OCRResult:
        raise ConversionError(ErrorCode.OCR_FAILED, "扫描页 OCR 失败")


@pytest.fixture
def ocr_provider(monkeypatch) -> RecordingOCR:
    provider = RecordingOCR()
    monkeypatch.setattr(providers, "_ocr", provider)
    return provider


def _run(
    key: str,
    input_path: Path,
    output_dir: Path,
    target_ext: str,
    options: dict,
) -> dict:
    return asyncio.run(
        PDF_HANDLERS[key].run(
            str(input_path), str(output_dir), target_ext, options,
        )
    )


def _make_scan_image(path: Path) -> Path:
    image = Image.new("RGB", (1200, 800), "white")
    ImageDraw.Draw(image).text((120, 320), "SCANNED PAGE 123", fill="black")
    image.save(path, "PNG")
    return path


def _make_scanned_pdf(path: Path) -> Path:
    image_path = _make_scan_image(path.with_suffix(".png"))
    with Image.open(image_path) as image:
        image.convert("RGB").save(path, "PDF", resolution=150)
    return path


def _make_native_pdf(path: Path) -> Path:
    pdf = canvas.Canvas(str(path), pagesize=A4)
    pdf.drawString(72, 760, "NATIVE PAGE TEXT")
    pdf.save()
    return path


def _make_mixed_pdf(path: Path) -> Path:
    scan_image = _make_scan_image(path.with_suffix(".png"))
    width, height = A4
    pdf = canvas.Canvas(str(path), pagesize=A4)
    pdf.drawString(72, 760, "NATIVE PAGE TEXT")
    pdf.showPage()
    pdf.drawImage(ImageReader(str(scan_image)), 0, 0, width=width, height=height)
    pdf.save()
    return path


def test_scanned_pdf_to_txt_uses_ocr_and_forwards_options(
    tmp_path: Path, ocr_provider: RecordingOCR,
) -> None:
    src = _make_scanned_pdf(tmp_path / "scan.pdf")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    result = _run(
        "pdf_to_txt",
        src,
        out_dir,
        ".txt",
        {"ocr_language": "zh", "detect_tables": False, "preserve_layout": True},
    )

    assert "扫描页 OCR 文字" in Path(result["output_path"]).read_text(encoding="utf-8")
    assert ocr_provider.calls == [
        {"language": "zh", "detect_tables": False, "preserve_layout": True}
    ]
    assert "OCR" in (result["quality_notice"] or "")
    assert not list(out_dir.glob("ocr-page-*.png"))


def test_scanned_pdf_to_docx_contains_editable_ocr_text(
    tmp_path: Path, ocr_provider: RecordingOCR,
) -> None:
    src = _make_scanned_pdf(tmp_path / "scan.pdf")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    result = _run("pdf_to_docx", src, out_dir, ".docx", {})

    text = "\n".join(p.text for p in Document(result["output_path"]).paragraphs)
    assert "扫描页 OCR 文字" in text
    assert len(ocr_provider.calls) == 1
    assert "OCR" in (result["quality_notice"] or "")


def test_mixed_pdf_keeps_page_order_and_ocrs_only_scan_page(
    tmp_path: Path, ocr_provider: RecordingOCR,
) -> None:
    src = _make_mixed_pdf(tmp_path / "mixed.pdf")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    result = _run("pdf_to_txt", src, out_dir, ".txt", {})

    text = Path(result["output_path"]).read_text(encoding="utf-8")
    assert text.index("NATIVE PAGE TEXT") < text.index("扫描页 OCR 文字")
    assert len(ocr_provider.calls) == 1


def test_pdf_ocr_failure_is_preserved_and_page_image_is_removed(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(providers, "_ocr", FailingOCR())
    src = _make_scanned_pdf(tmp_path / "scan.pdf")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    with pytest.raises(ConversionError) as exc:
        _run("pdf_to_txt", src, out_dir, ".txt", {})

    assert exc.value.code == ErrorCode.OCR_FAILED
    assert not list(out_dir.glob("ocr-page-*.png"))


def test_native_pdf_docx_does_not_call_ocr(
    tmp_path: Path, ocr_provider: RecordingOCR,
) -> None:
    src = _make_native_pdf(tmp_path / "native.pdf")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    result = _run("pdf_to_docx", src, out_dir, ".docx", {})

    Document(result["output_path"])
    assert len(ocr_provider.calls) == 0
