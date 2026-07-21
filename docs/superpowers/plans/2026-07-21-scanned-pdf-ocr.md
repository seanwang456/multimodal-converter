# Scanned PDF OCR Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make existing PDF-to-TXT and PDF-to-DOCX conversions produce editable text from scanned pages while preserving the native-text fast path.

**Architecture:** Inspect every PDF page with `pdfplumber`; pages without meaningful native text, or with only a small amount of text over a page-sized image, are rendered to PNG and passed through the existing `OCRProvider`. Merge native and OCR text in page order. Keep Registry, API routes, job state, storage isolation, and download behavior unchanged.

**Tech Stack:** Python 3.11, FastAPI handlers, pdfplumber/pypdfium2 rendering, existing OCRProvider, python-docx, pytest.

## Global Constraints

- The backend remains the only trusted validation source and continues resolving handlers from the Conversion Registry.
- Do not add per-conversion API endpoints or accept a frontend-supplied `handler_key`.
- OCR must be accessed only through `OCRProvider.extract_text(image_path, language, detect_tables, preserve_layout)`.
- Encrypted PDFs fail with `PASSWORD_PROTECTED_PDF` before rendering or OCR.
- PDF jobs keep the existing 20-minute timeout and per-job workdir cleanup.
- This change covers scanned PDF to `.txt` and `.docx`; `.pptx` and `.xlsx` behavior remains unchanged.

---

### Task 1: Add scanned and mixed PDF regression tests

**Files:**
- Create: `apps/api/tests/test_pdf_ocr.py`
- Reference: `apps/api/app/providers/base.py`
- Reference: `apps/api/app/handlers/pdf_handlers.py`

**Interfaces:**
- Consumes: `PDF_HANDLERS[key].run(input_path, output_dir, target_ext, options)` and injectable `app.providers._ocr`.
- Produces: failing behavioral tests for scanned TXT, scanned DOCX, mixed-page ordering, OCR options, OCR errors, and the native DOCX fast path.

- [ ] **Step 1: Create real PDF fixtures and a recording OCR provider**

```python
class RecordingOCR(OCRProvider):
    def __init__(self, text: str = "扫描页 OCR 文字") -> None:
        self.text = text
        self.calls: list[dict] = []

    async def extract_text(self, image_path: str, language="auto", detect_tables=True, preserve_layout=False):
        assert Path(image_path).suffix == ".png"
        self.calls.append({
            "language": language,
            "detect_tables": detect_tables,
            "preserve_layout": preserve_layout,
        })
        return {"text": self.text, "tables": [], "confidence": 0.9}
```

Use Pillow to generate a one-page image-only PDF. Use ReportLab to generate a two-page mixed PDF whose first page contains native text and whose second page contains a full-page raster image.

- [ ] **Step 2: Write the scanned PDF to TXT and DOCX tests**

```python
def test_scanned_pdf_to_txt_uses_ocr_and_forwards_options(tmp_path, ocr_provider):
    result = _run("pdf_to_txt", scanned_pdf, out_dir, ".txt", options)
    assert "扫描页 OCR 文字" in Path(result["output_path"]).read_text("utf-8")
    assert ocr_provider.calls == [{
        "language": "zh", "detect_tables": False, "preserve_layout": True,
    }]

def test_scanned_pdf_to_docx_contains_editable_ocr_text(tmp_path, ocr_provider):
    result = _run("pdf_to_docx", scanned_pdf, out_dir, ".docx", {})
    text = "\n".join(p.text for p in Document(result["output_path"]).paragraphs)
    assert "扫描页 OCR 文字" in text
```

- [ ] **Step 3: Write mixed-page, error, and native fast-path tests**

```python
def test_mixed_pdf_keeps_page_order_and_ocrs_only_scan_page(tmp_path, ocr_provider):
    src = _make_mixed_pdf(tmp_path / "mixed.pdf")
    out_dir = tmp_path / "out"; out_dir.mkdir()
    result = _run("pdf_to_txt", src, out_dir, ".txt", {})
    text = Path(result["output_path"]).read_text("utf-8")
    assert text.index("NATIVE PAGE TEXT") < text.index("扫描页 OCR 文字")
    assert len(ocr_provider.calls) == 1

def test_pdf_ocr_failure_is_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr(providers, "_ocr", FailingOCR())
    src = _make_scanned_pdf(tmp_path / "scan.pdf")
    out_dir = tmp_path / "out"; out_dir.mkdir()
    with pytest.raises(ConversionError) as exc:
        _run("pdf_to_txt", src, out_dir, ".txt", {})
    assert exc.value.code == ErrorCode.OCR_FAILED

def test_native_pdf_docx_does_not_call_ocr(tmp_path, ocr_provider):
    src = _make_native_pdf(tmp_path / "native.pdf")
    out_dir = tmp_path / "out"; out_dir.mkdir()
    _run("pdf_to_docx", src, out_dir, ".docx", {})
    assert len(ocr_provider.calls) == 0
```

- [ ] **Step 4: Run the new tests and verify RED**

Run: `cd apps/api && uv run --frozen pytest tests/test_pdf_ocr.py -q`

Expected: scanned TXT contains no OCR text and scanned DOCX contains no editable OCR text, proving the missing behavior is reproduced.

### Task 2: Implement page inspection, rendering, and OCR aggregation

**Files:**
- Modify: `apps/api/app/handlers/pdf_handlers.py`
- Test: `apps/api/tests/test_pdf_ocr.py`

**Interfaces:**
- Produces: `_inspect_pdf_pages(input_path: str) -> list[PdfPageState]`, `_render_pdf_page(input_path: str, page_index: int, dest: Path) -> None`, and async `_extract_pdf_text_with_ocr(input_path: str, output_dir: Path, options: dict[str, Any], pages: list[PdfPageState] | None = None) -> PdfTextResult`.
- Consumes: `get_ocr_provider()` and `OCRProvider.extract_text(image_path, language, detect_tables, preserve_layout)`.

- [ ] **Step 1: Add focused page state and detection helpers**

```python
@dataclass(frozen=True)
class PdfPageState:
    index: int
    native_text: str
    needs_ocr: bool

@dataclass(frozen=True)
class PdfTextResult:
    text: str
    ocr_pages: int

def _has_page_sized_image(page) -> bool:
    page_area = float(page.width * page.height)
    return any(
        max(0.0, float(img.get("x1", 0) - img.get("x0", 0)))
        * max(0.0, float(img.get("bottom", 0) - img.get("top", 0)))
        / page_area >= 0.5
        for img in page.images
    ) if page_area else False

def _page_needs_ocr(page, text: str) -> bool:
    compact = "".join(text.split())
    return not compact or (len(compact) < 20 and _has_page_sized_image(page))
```

- [ ] **Step 2: Render one page through pdfplumber's public rendering API**

```python
def _render_pdf_page(input_path: str, page_index: int, dest: Path) -> None:
    import pdfplumber
    with pdfplumber.open(input_path) as pdf:
        pdf.pages[page_index].to_image(resolution=250, antialias=True).save(
            str(dest), format="PNG"
        )
```

- [ ] **Step 3: Aggregate native and OCR text without retaining temporary images**

```python
async def _extract_pdf_text_with_ocr(
    input_path: str,
    output_dir: Path,
    options: dict[str, Any],
    pages: list[PdfPageState] | None = None,
) -> PdfTextResult:
    states = pages or await asyncio.to_thread(_inspect_pdf_pages, input_path)
    provider = get_ocr_provider() if any(p.needs_ocr for p in states) else None
    parts: list[str] = []
    ocr_pages = 0
    for state in states:
        if not state.needs_ocr:
            parts.append(state.native_text.strip())
            continue
        image_path = output_dir / f"ocr-page-{state.index + 1}.png"
        try:
            await asyncio.to_thread(_render_pdf_page, input_path, state.index, image_path)
            result = await provider.extract_text(
                str(image_path),
                language=options.get("ocr_language", "auto"),
                detect_tables=options.get("detect_tables", True),
                preserve_layout=options.get("preserve_layout", False),
            )
            parts.append((result.get("text") or "").strip())
            ocr_pages += 1
        finally:
            image_path.unlink(missing_ok=True)
    return PdfTextResult(text="\n\n".join(parts).strip(), ocr_pages=ocr_pages)
```

Wrap rendering failures as `CONVERSION_ENGINE_ERROR` with a path-free message, while allowing `ConversionError` from the provider to propagate unchanged.

- [ ] **Step 4: Run helper-facing tests and verify they progress to handler failures**

Run: `cd apps/api && uv run --frozen pytest tests/test_pdf_ocr.py -q`

Expected: helper-level rendering and provider call assertions pass; TXT/DOCX handler output tests may still fail until Task 3.

### Task 3: Integrate OCR-aware text into PDF to TXT and DOCX

**Files:**
- Modify: `apps/api/app/handlers/pdf_handlers.py`
- Test: `apps/api/tests/test_pdf_ocr.py`

**Interfaces:**
- Consumes: `PdfTextResult`, `_inspect_pdf_pages`, `_extract_pdf_text_with_ocr` from Task 2.
- Produces: OCR-aware behavior in existing `PdfHandler.run()` without changing handler keys.

- [ ] **Step 1: Inspect pages once for TXT and DOCX**

```python
pages = await asyncio.to_thread(_inspect_pdf_pages, input_path)
has_scanned_pages = any(page.needs_ocr for page in pages)
```

Only execute this block for target extensions `.txt` and `.docx`; leave PPTX/XLSX code unchanged.

- [ ] **Step 2: Keep the native DOCX layout path and add the scanned DOCX path**

```python
if target_ext == ".docx" and not has_scanned_pages:
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
            raise ConversionError(
                ErrorCode.PASSWORD_PROTECTED_PDF,
                "PDF 已加密，请解除密码后重新上传",
            ) from e
        native_text = "\n\n".join(page.native_text for page in pages).strip()
        await asyncio.to_thread(write_docx, native_text, out)
        return ConversionResult(
            output_path=str(out), filename=out.name, size_bytes=out.stat().st_size,
            quality_notice="版式还原失败，已退化为纯文本 Word。",
        )

if target_ext == ".docx":
    extracted = await _extract_pdf_text_with_ocr(input_path, out_dir, options, pages)
    await asyncio.to_thread(write_docx, extracted.text, out)
    return ConversionResult(
        output_path=str(out), filename=out.name, size_bytes=out.stat().st_size,
        quality_notice=_ocr_quality_notice(extracted),
    )
```

- [ ] **Step 3: Make PDF to TXT OCR-aware**

```python
if target_ext == ".txt":
    extracted = await _extract_pdf_text_with_ocr(input_path, out_dir, options, pages)
    await asyncio.to_thread(out.write_text, extracted.text, "utf-8")
    return ConversionResult(
        output_path=str(out), filename=out.name, size_bytes=out.stat().st_size,
        quality_notice=_ocr_quality_notice(extracted),
    )
```

Use the notice `扫描页面经 OCR 识别，复杂版面、手写内容或低清晰度页面可能存在误差。` when `ocr_pages > 0`; append `未识别到有效文字。` when the merged text is empty.

- [ ] **Step 4: Run the new test file and verify GREEN**

Run: `cd apps/api && uv run --frozen pytest tests/test_pdf_ocr.py -q`

Expected: all tests pass.

### Task 4: Regression verification and documentation

**Files:**
- Modify: `README.md`

**Interfaces:**
- Documents that scanned and mixed PDFs use the configured OCR Provider for TXT/DOCX output.

- [ ] **Step 1: Update README capability and provider notes**

Add these exact points to the existing README sections:

```markdown
- **扫描 PDF**：文字型、扫描型及混合 PDF → 可编辑 TXT / Word；扫描页自动使用 OCR Provider

PDF 页面没有可用文本层时会自动转为图片并调用 OCR。此时必须配置
`LLM_BASE_URL`、`LLM_API_KEY` 和 `LLM_VISION_MODEL`；普通文字型 PDF 不调用 OCR。
```

- [ ] **Step 2: Run focused and full backend tests**

Run: `cd apps/api && uv run --frozen pytest tests/test_pdf_ocr.py tests/test_phase4.py tests/test_phase34_fixes.py -q`

Expected: all selected tests pass.

Run: `cd apps/api && uv run --frozen pytest -q`

Expected: the new total passes with only the pre-existing environment-dependent skip.

- [ ] **Step 3: Build the frontend and check repository state**

Run: `cd apps/web && pnpm build`

Expected: production build succeeds.

Run: `git diff --check && git status --short`

Expected: no whitespace errors; only intended source, test, README, and plan changes are present.
