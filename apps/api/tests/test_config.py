from __future__ import annotations

import pytest

from app.config import _get_bounded_int


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", 1),
        ("3", 3),
        ("8", 8),
        ("0", 1),
        ("-9", 1),
        ("9", 8),
        ("not-an-int", 3),
    ],
)
def test_pdf_ocr_page_concurrency_is_bounded(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: int,
) -> None:
    monkeypatch.setenv("PDF_OCR_PAGE_CONCURRENCY", raw)

    assert _get_bounded_int("PDF_OCR_PAGE_CONCURRENCY", 3, 1, 8) == expected


def test_pdf_ocr_page_concurrency_defaults_to_three(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PDF_OCR_PAGE_CONCURRENCY", raising=False)

    assert _get_bounded_int("PDF_OCR_PAGE_CONCURRENCY", 3, 1, 8) == 3
