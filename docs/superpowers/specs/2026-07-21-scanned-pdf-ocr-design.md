# 扫描型 PDF OCR 转换设计

## 目标

让现有 `.pdf → .txt` 与 `.pdf → .docx` 转换同时支持文字型 PDF、扫描型 PDF 和二者混合的 PDF。扫描页面经 OCR 后输出可编辑文字；不新增 API、不新增 per-route handler，也不信任前端传入的 handler key。

## 现状与根因

- `pdf_to_txt` 仅使用 `pdfplumber.extract_text()`，扫描页没有文本层，因此得到空文本。
- `pdf_to_docx` 优先使用 `pdf2docx`。扫描页会被作为图片写入 Word，转换本身不会失败，所以现有异常回退无法触发 OCR。
- 项目已有 `OCRProvider.extract_text(...)`，但 PDF handler 尚未调用它。

## 方案选择

采用“逐页原生文本检测 + 按需 OCR”：

1. 使用 PDF 解析库逐页提取原生文本。
2. 页面包含足够的非空白文字时直接保留原生文本。
3. 页面没有有效文本时，以 250 DPI 左右渲染为 PNG。
4. 将 PNG 交给现有 `OCRProvider`，传递 `ocr_language`、`detect_tables` 和 `preserve_layout` 选项。
5. 按原页序合并文本，并保留页间分隔。
6. `.txt` 写入 UTF-8 文本；`.docx` 使用现有 `write_docx()` 生成可编辑 Word。

该方案优于全量 OCR：文字型页面不产生额外模型费用和延迟，原生文字准确率也不会被 OCR 降低。它也比在现阶段引入 OCRmyPDF/Tesseract 更贴合现有 Provider 架构，避免新增 Ghostscript、Tesseract 和中文语言包等系统依赖。

## 组件边界

在 PDF handler 模块中增加独立的页面文本提取流程，其职责是：

- 检测每页是否有可用文本层；
- 只渲染需要 OCR 的页面；
- 通过 `get_ocr_provider()` 调用 Provider；
- 返回合并后的文本及是否使用过 OCR 的元数据。

`PdfHandler.run()` 继续负责加密预检、目标格式分支、结果文件生成和质量提示。Registry、路由、任务模型和下载逻辑不变。

## 转换行为

### PDF → TXT

- 文字页：直接使用原生文本。
- 扫描页：使用 OCR 文本。
- 混合 PDF：两类文本按页顺序合并。
- 所有页面均无可识别文字时仍生成 TXT，但质量提示说明 OCR 未识别到有效文字。

### PDF → DOCX

- 全部页面都有原生文本时，继续优先使用 `pdf2docx` 保留版式。
- 只要存在扫描页，就走逐页文本聚合并用 `write_docx()` 生成可编辑 Word，避免生成只有图片、没有文字的 Word。
- OCR Word 是 best-effort，不承诺还原原始版面。

### PDF → PPTX / XLSX

本次不扩展扫描型 PDF 到 PPTX/XLSX。现有行为保持不变，避免把表格结构识别和幻灯片排版纳入本次范围。

## 资源与安全

- 渲染图片写入当前 job 的独立 workdir，任务结束后沿用现有清理机制删除。
- 一次处理一页并在 OCR 后删除或复用页面图片，避免把整份大 PDF 同时加载到内存。
- 加密 PDF 继续在 OCR 前返回 `PASSWORD_PROTECTED_PDF`。
- OCR Provider 的异常保持为结构化 `OCR_FAILED`，不暴露文件路径或图片内容。
- PDF 类任务继续使用现有 20 分钟超时。

## 质量提示

- 未使用 OCR 的 Word 保留现有版式提示。
- 使用 OCR 的 TXT/Word 返回：“扫描页面经 OCR 识别，复杂版面、手写内容或低清晰度页面可能存在误差。”
- 混合 PDF 同样标记使用过 OCR，让用户知道部分页面是识别结果。

## 测试与验收

新增真实图片型 PDF 样本（在测试中生成）并通过 fake `OCRProvider` 隔离外部网络：

1. 扫描 PDF → TXT 会调用 OCR，并包含 OCR 返回文字。
2. 扫描 PDF → DOCX 会调用 OCR，生成的 Word 可打开且包含可编辑文字。
3. 混合 PDF 的原生页不调用 OCR，扫描页调用 OCR，输出顺序正确。
4. 文字型 PDF → DOCX 仍走现有版式转换路径。
5. OCR 选项正确传递给 Provider。
6. OCR 失败返回 `OCR_FAILED`。
7. 加密 PDF 仍在调用 OCR 前被拒绝。
8. 运行完整后端测试，确保现有 Registry、上传、任务和其他转换路线不回归。

## 不在本次范围

- 扫描 PDF → XLSX 的表格结构 OCR。
- 扫描 PDF → PPTX 的版式生成。
- 新增本地 PaddleOCR、Tesseract 或 OCRmyPDF Provider。
- 前端高级 OCR 选项 UI；后端会先支持已有 options 字段，前端后续可独立补充。
