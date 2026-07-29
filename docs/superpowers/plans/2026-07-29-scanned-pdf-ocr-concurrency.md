# Scanned PDF OCR Concurrency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace sequential scanned-page Provider calls with an ordered, bounded page worker pool whose deployment default is 3 concurrent OCR requests per PDF, while keeping PDFium rendering process-globally serialized.

**Architecture:** Keep page detection, the `OCRProvider` interface, and output handlers unchanged. Add a bounded configuration value and a PDF-local async worker pool that recognizes only scanned pages, stores results by original page index, stops assigning new work after a failure, and waits for in-flight render threads before cleanup. Every PDFium-backed `_render_pdf_page` call is protected by one process-level mutex because PDFium is globally not thread-safe; the mutex ends before `OCRProvider.extract_text(...)`, so Provider requests retain PDF-local bounded concurrency.

**Tech Stack:** Python 3.11, asyncio `TaskGroup`, `threading.Lock`, FastAPI settings, pdfplumber/pypdfium2, existing `OCRProvider`, pytest, Docker Compose.

## Global Constraints

- Default `PDF_OCR_PAGE_CONCURRENCY` is exactly `3`; accepted runtime range is `1–8`.
- The concurrency setting is deployment-only and must not be accepted from job options.
- Native-text pages must never be sent to OCR, and output must remain in original PDF page order.
- PDF → TXT and scanned PDF → DOCX share the concurrent extractor; pure-text PDF → DOCX remains on `pdf2docx`.
- `PDF_OCR_PAGE_CONCURRENCY` controls PDF-local OCR worker/Provider concurrency, not PDFium render concurrency.
- All PDFium-backed `_render_pdf_page` calls in one process are globally serialized; the render mutex must not cover `OCRProvider.extract_text(...)`.
- A page render error remains `CONVERSION_ENGINE_ERROR`; Provider errors such as `OCR_FAILED` retain their existing structured code.
- After one page fails, workers stop claiming new pages but allow already-started render/OCR work to finish and clean up.
- Render runs at the existing 250 DPI, and every `ocr-page-*.png` is removed on success, failure, or cancellation.
- The existing 20-minute PDF job timeout, Registry, routes, job schema, and frontend behavior do not change.
- Do not add Provider retries, multi-image prompts, global cross-job OCR/Provider limiting, or XLSX/PPTX OCR. The process-global PDFium render mutex is a required safety boundary and is not an OCR limiter.
- Never log page contents, image bytes, credentials, or absolute server paths.

---

### Task 1: Bounded deployment configuration

**Files:**
- Create: `apps/api/tests/test_config.py`
- Modify: `apps/api/app/config.py`

**Interfaces:**
- Consumes: environment variable `PDF_OCR_PAGE_CONCURRENCY`.
- Produces: `_get_bounded_int(name: str, default: int, minimum: int, maximum: int) -> int` and `Settings.pdf_ocr_page_concurrency: int`.

- [ ] **Step 1: Write failing boundary tests**

Create `apps/api/tests/test_config.py` with direct tests for the environment parser. Testing the helper avoids relying on dataclass defaults that are evaluated when `app.config` is imported.

```python
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
```

- [ ] **Step 2: Run the focused tests and verify the missing helper failure**

Run:

```bash
cd apps/api
uv run pytest tests/test_config.py -q
```

Expected: collection fails because `app.config` does not yet export `_get_bounded_int`.

- [ ] **Step 3: Implement the bounded integer parser and setting**

Add the helper beside `_get_int` in `apps/api/app/config.py`:

```python
def _get_bounded_int(
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value = _get_int(name, default)
    return min(max(value, minimum), maximum)
```

Add the setting beside `max_concurrent_jobs`:

```python
pdf_ocr_page_concurrency: int = _get_bounded_int(
    "PDF_OCR_PAGE_CONCURRENCY", 3, 1, 8,
)
```

- [ ] **Step 4: Run the configuration tests**

Run:

```bash
cd apps/api
uv run pytest tests/test_config.py -q
```

Expected: `8 passed`.

- [ ] **Step 5: Commit the bounded configuration**

```bash
git add apps/api/app/config.py apps/api/tests/test_config.py
git commit -m "feat: add bounded PDF OCR page concurrency setting"
```

---

### Task 2: Ordered page-level OCR worker pool

**Files:**
- Modify: `apps/api/tests/test_pdf_ocr.py`
- Modify: `apps/api/app/handlers/pdf_handlers.py`

**Interfaces:**
- Consumes: `settings.pdf_ocr_page_concurrency`, `PdfPageState`, `_render_pdf_page(...)`, and `OCRProvider.extract_text(...)`.
- Produces: process-safe serialized PDFium rendering inside `_render_pdf_page(...)`, `_render_pdf_page_for_ocr(input_path: str, page_index: int, dest: Path) -> None` as an async cancellation-safe adapter, and PDF-local concurrent Provider behavior inside `_extract_pdf_text_with_ocr(...) -> PdfTextResult`.

**Approved safety correction:** Root-cause testing showed that pypdfium2/PDFium crashes when called concurrently from different threads, including calls for different PDF files. Add an isolated subprocess regression that concurrently invokes the real production `_render_pdf_page`; it must exit 0 and produce three non-empty PNGs. Protect all PDFium render calls with a clearly named process-level `threading.Lock`. Do not add a global OCR/Provider limiter.

- [ ] **Step 1: Add a controllable fake Provider and fake renderer**

Extend `apps/api/tests/test_pdf_ocr.py` imports:

```python
import subprocess
import sys
import textwrap
import threading
from types import SimpleNamespace

import app.handlers.pdf_handlers as pdf_handlers
```

Add a Provider that records overlap, page order, optional failure, and an optional start barrier. The page number is derived from the existing `ocr-page-{number}.png` filename. The barrier lets the real-render integration test prove Provider concurrency deterministically even though rendering is serialized.

```python
class CoordinatedOCR(OCRProvider):
    def __init__(
        self,
        delays: dict[int, float] | None = None,
        fail_pages: set[int] | None = None,
        wait_for_started: int | None = None,
    ) -> None:
        self.delays = delays or {}
        self.fail_pages = fail_pages or set()
        self.wait_for_started = wait_for_started
        self._started_barrier = asyncio.Event()
        self.started: list[int] = []
        self.active = 0
        self.max_active = 0

    async def extract_text(
        self,
        image_path: str,
        language: str = "auto",
        detect_tables: bool = True,
        preserve_layout: bool = False,
    ) -> OCRResult:
        page_number = int(Path(image_path).stem.rsplit("-", 1)[1])
        self.started.append(page_number)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if (
                self.wait_for_started is not None
                and len(self.started) >= self.wait_for_started
            ):
                self._started_barrier.set()
            if self.wait_for_started is not None:
                await self._started_barrier.wait()
            await asyncio.sleep(self.delays.get(page_number, 0.03))
            if page_number in self.fail_pages:
                raise ConversionError(ErrorCode.OCR_FAILED, f"第 {page_number} 页 OCR 失败")
            return OCRResult(
                text=f"OCR PAGE {page_number}", tables=[], confidence=0.9,
            )
        finally:
            self.active -= 1


def _fake_render_pdf_page(
    input_path: str, page_index: int, dest: Path,
) -> None:
    dest.write_bytes(f"page-{page_index + 1}".encode())


def _ocr_states(count: int) -> list[pdf_handlers.PdfPageState]:
    return [
        pdf_handlers.PdfPageState(index=index, native_text="", needs_ocr=True)
        for index in range(count)
    ]
```

- [ ] **Step 1a: Write the isolated subprocess PDFium safety regression**

Create a real three-page image-only PDF in the parent pytest process. Launch `sys.executable -c ...` with `cwd=apps/api`; inside the child, concurrently call the production `_render_pdf_page` through three `asyncio.to_thread` calls. Assert child exit code `0`, exactly three output PNGs, and every PNG size greater than zero. Before the process-level mutex exists, the isolated child must terminate with a native signal/non-zero exit without taking down pytest itself.

This regression exercises the real pdfplumber/pypdfium2 renderer. Do not monkeypatch the renderer or assert on the lock object.

- [ ] **Step 2: Write failing concurrency and order tests**

Add a parameterized test that invokes the shared extractor directly, keeping the test fast and independent of PDF rendering:

```python
@pytest.mark.parametrize(("limit", "expected_peak"), [(1, 1), (2, 2), (3, 3)])
def test_pdf_ocr_respects_page_concurrency_and_keeps_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit: int,
    expected_peak: int,
) -> None:
    provider = CoordinatedOCR(
        delays={1: 0.08, 2: 0.06, 3: 0.04, 4: 0.02, 5: 0.01},
    )
    monkeypatch.setattr(providers, "_ocr", provider)
    monkeypatch.setattr(pdf_handlers, "_render_pdf_page", _fake_render_pdf_page)
    monkeypatch.setattr(
        pdf_handlers,
        "settings",
        SimpleNamespace(pdf_ocr_page_concurrency=limit),
    )

    result = asyncio.run(
        pdf_handlers._extract_pdf_text_with_ocr(
            "unused.pdf", tmp_path, {}, _ocr_states(5),
        )
    )

    assert provider.max_active == expected_peak
    assert result.text.split("\n\n") == [f"OCR PAGE {n}" for n in range(1, 6)]
    assert result.ocr_pages == 5
    assert not list(tmp_path.glob("ocr-page-*.png"))
```

- [ ] **Step 3: Write a failing stop-on-error and cleanup test**

```python
def test_pdf_ocr_failure_stops_new_pages_and_cleans_inflight_images(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = CoordinatedOCR(
        delays={1: 0.08, 2: 0.01, 3: 0.08},
        fail_pages={2},
    )
    monkeypatch.setattr(providers, "_ocr", provider)
    monkeypatch.setattr(pdf_handlers, "_render_pdf_page", _fake_render_pdf_page)
    monkeypatch.setattr(
        pdf_handlers,
        "settings",
        SimpleNamespace(pdf_ocr_page_concurrency=3),
    )

    with pytest.raises(ConversionError) as exc:
        asyncio.run(
            pdf_handlers._extract_pdf_text_with_ocr(
                "unused.pdf", tmp_path, {}, _ocr_states(7),
            )
        )

    assert exc.value.code == ErrorCode.OCR_FAILED
    assert set(provider.started) == {1, 2, 3}
    assert not list(tmp_path.glob("ocr-page-*.png"))
```

- [ ] **Step 4: Write a failing cancellation-versus-render-thread test**

This test makes the renderer write once before cancellation and once after it is released. The extractor must remain pending until the thread exits and then remove the final file.

```python
def test_pdf_ocr_cancellation_waits_for_render_thread_before_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = RecordingOCR()
    render_started = threading.Event()
    allow_render_finish = threading.Event()
    render_finished = threading.Event()

    def blocking_render(input_path: str, page_index: int, dest: Path) -> None:
        dest.write_bytes(b"render-started")
        render_started.set()
        assert allow_render_finish.wait(timeout=2)
        dest.write_bytes(b"render-finished")
        render_finished.set()

    monkeypatch.setattr(providers, "_ocr", provider)
    monkeypatch.setattr(pdf_handlers, "_render_pdf_page", blocking_render)
    monkeypatch.setattr(
        pdf_handlers,
        "settings",
        SimpleNamespace(pdf_ocr_page_concurrency=3),
    )

    async def cancel_during_render() -> bool:
        task = asyncio.create_task(
            pdf_handlers._extract_pdf_text_with_ocr(
                "unused.pdf", tmp_path, {}, _ocr_states(1),
            )
        )
        assert await asyncio.to_thread(render_started.wait, 2)
        task.cancel()
        await asyncio.sleep(0.02)
        waited_for_thread = not task.done()
        allow_render_finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await asyncio.to_thread(render_finished.wait, 2)
        return waited_for_thread

    assert asyncio.run(cancel_during_render())
    assert provider.calls == []
    assert not list(tmp_path.glob("ocr-page-*.png"))
```

- [ ] **Step 5: Run the new tests and verify sequential behavior fails**

Run:

```bash
cd apps/api
uv run pytest tests/test_pdf_ocr.py -q
```

Expected: the isolated PDFium subprocess test exits non-zero due to the native threading violation. The existing sequential extractor also fails the concurrency assertions because `provider.max_active` remains 1, and it continues processing pages after a failure model that expects a bounded work pool.

- [ ] **Step 6: Add cancellation-safe threaded rendering**

Import the shared settings object and threading support, create the process-level PDFium mutex, and create a module logger in `apps/api/app/handlers/pdf_handlers.py`:

```python
import threading

from app.config import settings

_PDFIUM_RENDER_LOCK = threading.Lock()
log = logging.getLogger(__name__)
```

Protect the complete PDFium-backed render operation. The lock is process-global and intentionally covers calls from different jobs and different PDFs, because PDFium is globally not thread-safe:

```python
def _render_pdf_page(input_path: str, page_index: int, dest: Path) -> None:
    import pdfplumber

    with _PDFIUM_RENDER_LOCK:
        with pdfplumber.open(input_path) as pdf:
            page_image = pdf.pages[page_index].to_image(
                resolution=250, antialias=True,
            )
            page_image.save(str(dest), format="PNG")
```

Add this adapter immediately after `_render_pdf_page`:

```python
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
```

The shield prevents cancellation from cancelling the asyncio wrapper while the underlying thread keeps running or waits for `_PDFIUM_RENDER_LOCK`. Waiting for `render_task` before propagating cancellation makes the later `unlink` safe. `_PDFIUM_RENDER_LOCK` is released before `provider.extract_text(...)`, so this safety correction does not serialize Provider requests.

- [ ] **Step 7: Replace the sequential loop with the bounded worker pool**

Refactor `_extract_pdf_text_with_ocr` so native text is prefilled, OCR pages are queued by list position, and results are stored by `state.index`. Use the following implementation shape:

```python
states = pages if pages is not None else await asyncio.to_thread(
    _inspect_pdf_pages, input_path,
)
ocr_states = [state for state in states if state.needs_ocr]
provider = get_ocr_provider() if ocr_states else None
parts = [state.native_text.strip() if not state.needs_ocr else "" for state in states]

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
```

There is no await between reading and incrementing `next_position`, so workers cannot claim the same list entry on one event loop. `TaskGroup` waits for all child cleanup during external cancellation.

- [ ] **Step 8: Run the focused OCR tests**

Run:

```bash
cd apps/api
uv run pytest tests/test_pdf_ocr.py -q
```

Expected: all PDF OCR tests pass; the parameterized test observes peaks 1, 2, and 3 and ordered output.

- [ ] **Step 9: Add multi-page DOCX output-order coverage**

Add a helper that creates three image-only pages using the existing scan image:

```python
def _make_multi_page_scanned_pdf(path: Path, page_count: int = 3) -> Path:
    scan_image = _make_scan_image(path.with_suffix(".png"))
    width, height = A4
    pdf = canvas.Canvas(str(path), pagesize=A4)
    for _ in range(page_count):
        pdf.drawImage(ImageReader(str(scan_image)), 0, 0, width=width, height=height)
        pdf.showPage()
    pdf.save()
    return path
```

Add the integration test. It uses real page inspection and rendering but the controlled Provider:

```python
def test_multi_page_scanned_pdf_to_docx_keeps_page_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = CoordinatedOCR(wait_for_started=3)
    monkeypatch.setattr(providers, "_ocr", provider)
    monkeypatch.setattr(
        pdf_handlers,
        "settings",
        SimpleNamespace(pdf_ocr_page_concurrency=3),
    )
    src = _make_multi_page_scanned_pdf(tmp_path / "multi-scan.pdf")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    result = _run("pdf_to_docx", src, out_dir, ".docx", {})

    text = "\n".join(p.text for p in Document(result["output_path"]).paragraphs)
    assert text.index("OCR PAGE 1") < text.index("OCR PAGE 2")
    assert text.index("OCR PAGE 2") < text.index("OCR PAGE 3")
    assert provider.max_active == 3
    assert not list(out_dir.glob("ocr-page-*.png"))
```

- [ ] **Step 10: Run the complete PDF OCR test module again**

Run:

```bash
cd apps/api
uv run pytest tests/test_pdf_ocr.py -q
```

Expected: all tests pass, including the isolated real-render subprocess and real multi-page PDF → DOCX generation. The Provider barrier observes `max_active == 3` even though PDFium rendering is process-globally serialized.

- [ ] **Step 11: Commit the concurrent extractor**

```bash
git add apps/api/app/handlers/pdf_handlers.py apps/api/tests/test_pdf_ocr.py
git commit -m "feat: process scanned PDF pages concurrently"
```

---

### Task 3: Deployment guidance and release verification

**Files:**
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `DEPLOY.md`

**Interfaces:**
- Consumes: `PDF_OCR_PAGE_CONCURRENCY=3` and existing `MAX_CONCURRENT_JOBS=4`.
- Produces: operator-visible configuration, capacity guidance, and a verified Docker image.

- [ ] **Step 1: Document the new environment variable in `.env.example`**

Add this immediately after `MAX_CONCURRENT_JOBS=4`:

```dotenv
# 单份扫描 PDF 同时进行 OCR 的页数；范围 1-8，默认 3
PDF_OCR_PAGE_CONCURRENCY=3
```

- [ ] **Step 2: Add README configuration and behavior guidance**

Add this row to the README environment-variable table:

```markdown
| `PDF_OCR_PAGE_CONCURRENCY` | 单份扫描 PDF 的页级 OCR 并发，范围 1–8，默认 3 |
```

Extend the scanned PDF paragraph with this explicit capacity note:

```markdown
扫描页默认每份 PDF 同时识别 3 页，可通过 `PDF_OCR_PAGE_CONCURRENCY` 调整。该值会与 `MAX_CONCURRENT_JOBS` 相乘；默认单 worker 理论峰值为 12 个 PDF OCR 请求。模型限流或内存紧张时可降为 1–2。
```

- [ ] **Step 3: Update the operator handoff**

Add this row to the `DEPLOY.md` environment table:

```markdown
| `PDF_OCR_PAGE_CONCURRENCY` | 可选 | 单份扫描 PDF 页级 OCR 并发，范围 1–8，默认 3 |
```

Replace the concurrency limitation item with:

```markdown
2. **并发**：任务并发默认 4（`MAX_CONCURRENT_JOBS`）；单份扫描 PDF 页级 OCR 并发默认 3（`PDF_OCR_PAGE_CONCURRENCY`），单 worker 理论峰值为 12 个 PDF OCR 请求。模型服务限流、内存较小或 LibreOffice 负载较重时，优先将两者分别降为 1–2 和 2。
```

Add `PDF_OCR_PAGE_CONCURRENCY` to the deployment checklist so the operator explicitly confirms it matches the model quota and server memory.

- [ ] **Step 4: Check documentation and configuration diffs**

Run:

```bash
git diff --check
rg -n "PDF_OCR_PAGE_CONCURRENCY|理论峰值" .env.example README.md DEPLOY.md
```

Expected: no whitespace errors; every file documents default 3 and only README/DEPLOY state the default theoretical peak of 12.

- [ ] **Step 5: Run the complete backend test suite**

Run:

```bash
cd apps/api
uv run pytest -q
```

Expected: all backend tests pass, with the same intentional skip count as the branch baseline.

- [ ] **Step 6: Commit operator documentation**

```bash
git add .env.example README.md DEPLOY.md
git commit -m "docs: document scanned PDF OCR page concurrency"
```

- [ ] **Step 7: Rebuild and start the actual Docker services**

From the worktree root, run:

```bash
docker compose build api worker
docker compose up -d --force-recreate api worker
docker compose up -d --wait
docker compose ps
```

Expected: the API is healthy, worker is running, and both use the newly built API image. Do not print `.env` or any credential values.

- [ ] **Step 8: Confirm runtime configuration without exposing credentials**

Run:

```bash
docker compose exec worker python -c "from app.config import settings; print({'pdf_ocr_page_concurrency': settings.pdf_ocr_page_concurrency, 'ocr_configured': bool(settings.llm_base_url and settings.llm_api_key)})"
```

Expected: `pdf_ocr_page_concurrency` is `3` unless the deployment intentionally overrides it; `ocr_configured` must be `True` before the real Provider smoke test.

- [ ] **Step 9: Create a real four-page scanned PDF test input**

Use the API project environment to generate four image-only pages under a temporary directory. The generated PDF must have no native text layer.

```bash
cd apps/api
uv run python - <<'PY'
from pathlib import Path
from tempfile import gettempdir
from PIL import Image, ImageDraw, ImageFont

root = Path(gettempdir()) / "converter-pdf-ocr-concurrency"
root.mkdir(parents=True, exist_ok=True)
font_path = "/System/Library/Fonts/Helvetica.ttc"
font = ImageFont.truetype(font_path, 64)
pages = []
for number in range(1, 5):
    image = Image.new("RGB", (1654, 2339), "white")
    ImageDraw.Draw(image).text(
        (160, 900), f"SCANNED OCR PAGE {number}", fill="black", font=font,
    )
    pages.append(image)
dest = root / "four-page-scan.pdf"
pages[0].save(dest, "PDF", save_all=True, append_images=pages[1:], resolution=150)
print(dest)
PY
```

Expected: `/tmp/converter-pdf-ocr-concurrency/four-page-scan.pdf` exists and is non-empty. If the macOS Helvetica collection is unavailable, select an installed font reported by `system_profiler SPFontsDataType`; do not change application dependencies for the smoke fixture.

- [ ] **Step 10: Submit real TXT and DOCX jobs through the public API**

Run this from `apps/api`. It uploads the PDF, polls two real asynchronous jobs, downloads both artifacts, and validates that they are non-empty and readable:

```bash
uv run python - <<'PY'
import time
from pathlib import Path
import httpx
from docx import Document

root = Path("/tmp/converter-pdf-ocr-concurrency")
source = root / "four-page-scan.pdf"
client = httpx.Client(base_url="http://localhost:8000", timeout=180)
with source.open("rb") as stream:
    response = client.post(
        "/api/files",
        files={"file": (source.name, stream, "application/pdf")},
    )
response.raise_for_status()
file_id = response.json()["file_id"]

for target, filename in ((".txt", "result.txt"), (".docx", "result.docx")):
    response = client.post(
        "/api/jobs",
        json={"file_id": file_id, "target_ext": target, "options": {}},
    )
    response.raise_for_status()
    job_id = response.json()["job_id"]
    deadline = time.monotonic() + 1200
    while True:
        state_response = client.get(f"/api/jobs/{job_id}")
        state_response.raise_for_status()
        state = state_response.json()
        if state["status"] == "succeeded":
            break
        if state["status"] == "failed":
            raise RuntimeError(state["error"])
        if time.monotonic() >= deadline:
            raise TimeoutError(job_id)
        time.sleep(1)
    artifact = client.get(f"/api/jobs/{job_id}/download")
    artifact.raise_for_status()
    dest = root / filename
    dest.write_bytes(artifact.content)
    assert dest.stat().st_size > 0

text = (root / "result.txt").read_text(encoding="utf-8")
docx_text = "\n".join(
    paragraph.text for paragraph in Document(root / "result.docx").paragraphs
)
for artifact_text in (text, docx_text):
    page_positions = [artifact_text.index(str(number)) for number in range(1, 5)]
    assert page_positions == sorted(page_positions)
print({"txt_chars": len(text), "docx_chars": len(docx_text)})
PY
```

Expected: both jobs succeed; `result.txt` is valid UTF-8, `result.docx` opens with python-docx, both contain page numbers 1–4, and no secret is printed.

- [ ] **Step 11: Verify concurrent execution and clean temporary page images**

Run:

```bash
docker compose logs --since=15m worker | rg "PDF OCR 启动：扫描页 4，页级并发 3"
docker compose exec worker sh -lc 'find /app/storage/workdirs -name "ocr-page-*.png" -print'
```

Expected: the worker log reports 4 scanned pages with OCR worker concurrency 3, and `find` prints no files. The automated barrier-backed `CoordinatedOCR` test is the timing-level proof that Provider calls overlap; the Docker log proves the production path selected a three-worker pool. Neither output implies concurrent PDFium rendering, which remains process-globally serialized.

- [ ] **Step 12: Inspect real artifacts and recent logs**

Open `/tmp/converter-pdf-ocr-concurrency/result.txt` and `/tmp/converter-pdf-ocr-concurrency/result.docx`, confirm page order 1 → 4, then check:

```bash
docker compose logs --since=15m api worker | rg -i "error|traceback|ocr_failed|conversion_engine_error" || true
git status --short --branch
```

Expected: no conversion error or traceback from the smoke jobs, artifacts are ordered and editable, and the worktree contains no uncommitted implementation changes.

---

## Final verification checklist

- [ ] `PDF_OCR_PAGE_CONCURRENCY` defaults to 3 and clamps to 1–8.
- [ ] Unit tests prove page OCR overlaps and never exceeds the configured limit.
- [ ] An isolated subprocess test proves three concurrent production `_render_pdf_page` calls exit cleanly and create three non-empty PNGs under the process-level PDFium mutex.
- [ ] The real multi-page DOCX integration test proves `OCRProvider` still reaches `max_active == 3` while PDFium rendering is serialized.
- [ ] Mixed/native behavior and page order remain unchanged.
- [ ] Failure stops undispatched work and cleans all started page images.
- [ ] Cancellation during threaded rendering waits for the thread and leaves no page image behind.
- [ ] TXT and DOCX real artifacts are readable, editable, non-empty, and ordered.
- [ ] Full backend test suite passes.
- [ ] API and worker images are rebuilt and running the new code.
- [ ] README, `.env.example`, and `DEPLOY.md` explain capacity multiplication and rollback-to-serial configuration.
- [ ] No credentials, generated OCR page images, or unrelated user changes are committed.
