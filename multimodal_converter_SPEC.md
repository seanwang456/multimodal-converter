# 多模态文件转换 Web 应用 SPEC

> 版本：v0.1  
> 目标：快速完成一个可用的 Web 应用 MVP  
> 来源：基于用户上传的《多模态转换.docx》整理  
> 核心形态：上传文件 → 识别源格式 → 展示可转换目标 → 创建异步任务 → 转换 → 下载结果

---

## 1. 背景

本项目需要开发一个 Web 应用，支持办公文档、图片、音频、视频的基础格式转换与多模态转换。

原始需求文档中定义了以下内容：

- 支持的文件类型与大小限制
- 多模态转换能力
- 文档格式互转矩阵
- 图片格式互转矩阵
- 音频格式互转矩阵
- 视频转文本规则与注意事项

本 SPEC 将其整理为适合 vibe coding / agent coding 的开发说明。

---

## 2. 产品目标

开发一个简单可用的多模态文件转换 Web 应用。

用户流程：

1. 用户上传文件。
2. 系统识别文件格式和大小。
3. 系统根据源格式展示可转换的目标格式。
4. 用户选择目标格式和转换选项。
5. 系统创建异步转换任务。
6. 前端轮询任务状态。
7. 转换成功后用户下载结果。
8. 转换失败时展示明确失败原因。

---

## 3. MVP 范围

### 3.1 必须实现

- 文件上传
- 文件类型校验
- 文件大小限制校验
- 源格式识别
- 合法目标格式展示
- 转换任务创建
- 异步转换任务执行
- 任务状态查询
- 转换结果下载
- 转换失败原因展示
- 最近转换任务列表
- 后端统一 conversion registry
- 转换 handler 抽象
- 基础安全处理

### 3.2 暂不实现

- 登录注册
- 支付/会员/积分
- 多用户权限隔离
- 长期云存储
- 文件永久保留
- 高级排版还原
- 精确说话人分离
- 视频画面 OCR
- 批量文件转换
- 文件夹上传
- 企业管理后台

---

## 4. 技术架构

### 4.1 推荐架构

```text
Next.js Web 前端
        ↓
FastAPI 转换服务
        ↓
本地文件存储 / S3-compatible 存储
        ↓
异步任务队列
        ↓
转换 Handler
```

### 4.2 推荐技术栈

前端：

- Next.js
- React
- TypeScript
- Tailwind CSS
- shadcn/ui

后端：

- Python
- FastAPI
- SQLite / PostgreSQL
- FastAPI BackgroundTasks，后续可替换为 Redis + RQ / Celery
- 本地文件系统存储，后续可替换为 OSS / S3

转换工具：

- LibreOffice headless：Office 文档转换
- ffmpeg：音频/视频处理
- Pillow：图片格式转换
- python-docx：生成 Word
- reportlab / WeasyPrint：生成 PDF
- openpyxl：生成 Excel
- python-pptx：生成 PPTX
- OCR Provider：图片/扫描件转文字
- ASR Provider：音频/视频转文字

---

## 5. 核心设计原则

### 5.1 不要为每条转换路线单独写接口

错误方式：

```text
POST /pdf-to-word
POST /pdf-to-ppt
POST /jpg-to-docx
POST /mp3-to-text
```

正确方式：

```text
POST /api/files
POST /api/jobs
GET /api/jobs/{job_id}
GET /api/jobs/{job_id}/download
```

通过统一参数表达转换需求：

```json
{
  "file_id": "file_xxx",
  "target_ext": ".docx",
  "options": {}
}
```

### 5.2 必须使用统一 Conversion Registry

核心抽象：

```text
源格式 + 目标格式 → handler_key → 转换处理器
```

示例：

```text
.pdf + .docx → pdf_to_docx
.jpg + .txt → image_ocr_to_txt
.mp4 + .docx → video_asr_to_docx
```

### 5.3 后端是唯一可信校验源

前端可以展示转换矩阵，但不能作为安全依据。

后端必须再次校验：

- 文件格式是否支持
- 文件大小是否合规
- 源格式与目标格式是否允许转换
- handler 是否存在
- 任务状态是否合法

---

## 6. 文件类型与大小限制

### 6.1 办公文档

| 文件类型 | 支持格式 | 最大限制 |
|---|---|---:|
| PDF | `.pdf` | 50MB |
| Word | `.docx`, `.doc` | 30MB |
| PowerPoint | `.pptx`, `.ppt` | 50MB |
| Excel | `.xlsx`, `.xls` | 30MB |
| 文本 | `.txt` | 10MB |

### 6.2 图片

| 文件类型 | 支持格式 | 最大限制 |
|---|---|---:|
| 图片 | `.jpg`, `.jpeg`, `.png`, `.bmp` | 20MB |

### 6.3 音频

| 文件类型 | 支持格式 | 最大限制 |
|---|---|---:|
| MP3 | `.mp3` | 100MB |
| WAV | `.wav` | 200MB |
| AAC | `.aac` | 100MB |
| M4A | `.m4a` | 100MB |

### 6.4 视频

| 文件类型 | 支持格式 | 最大限制 |
|---|---|---:|
| 视频 | `.mp4` | 500MB |

说明：

原始文档的视频转文本矩阵中出现了 `.avi`、`.mov`、`.mkv`，但「支持的全部文件类型」中只列出 `.mp4`。MVP 阶段以 `.mp4` 为准，后续再扩展 `.avi`、`.mov`、`.mkv`。

---

## 7. 转换能力矩阵

## 7.1 多模态转换

### 7.1.1 图片 / 扫描件 → 文档

适用场景：

将拍照、扫描的纸质文件或截图中的文字提取为可编辑电子文档。

| 源格式 | 目标格式 | 处理方式 |
|---|---|---|
| `.jpg`, `.jpeg` | `.txt` | OCR 提取文字 |
| `.jpg`, `.jpeg` | `.docx`, `.doc` | OCR 后生成 Word |
| `.jpg`, `.jpeg` | `.xlsx`, `.xls` | OCR 后尝试表格结构化 |
| `.jpg`, `.jpeg` | `.pdf` | OCR 后生成 PDF |
| `.jpg`, `.jpeg` | `.pptx`, `.ppt` | OCR 后生成简单 PPT |
| `.png` | `.txt` | OCR 提取文字 |
| `.png` | `.docx`, `.doc` | OCR 后生成 Word |
| `.png` | `.xlsx`, `.xls` | OCR 后尝试表格结构化 |
| `.png` | `.pdf` | OCR 后生成 PDF |
| `.png` | `.pptx`, `.ppt` | OCR 后生成简单 PPT |
| `.bmp` | `.txt`, `.docx`, `.xlsx`, `.pdf`, `.pptx` | OCR 后生成对应文档 |

MVP 建议：

- 首版只输出 `.txt`、`.docx`、`.pdf`
- `.xlsx` 仅在识别到明显表格时输出
- `.pptx` 可用简单模板生成，不承诺专业排版

---

### 7.1.2 音频 / 录音 → 文本类文档

适用场景：

将会议录音、培训录播、采访音频转换为可编辑文字稿。

| 源格式 | 目标格式 | 处理方式 |
|---|---|---|
| `.mp3` | `.txt` | ASR 转写 |
| `.mp3` | `.docx`, `.doc` | ASR 转写后生成 Word |
| `.mp3` | `.pdf` | ASR 转写后生成 PDF |
| `.wav` | `.txt` | ASR 转写 |
| `.wav` | `.docx`, `.doc` | ASR 转写后生成 Word |
| `.wav` | `.pdf` | ASR 转写后生成 PDF |
| `.m4a` | `.txt` | ASR 转写 |
| `.m4a` | `.docx`, `.doc` | ASR 转写后生成 Word |
| `.m4a` | `.pdf` | ASR 转写后生成 PDF |
| `.aac` | `.txt` | ASR 转写 |
| `.aac` | `.docx`, `.doc` | ASR 转写后生成 Word |
| `.aac` | `.pdf` | ASR 转写后生成 PDF |

输出规则：

- 默认带时间戳
- 可选择是否输出说话人切换提示
- MVP 不做精确说话人分离

---

### 7.1.3 视频 → 文本类文档

适用场景：

将培训视频、会议录像、宣传视频、网课录屏等视频文件中的语音内容提取并转换为可编辑文字稿。

| 源格式 | 目标格式 | 处理方式 |
|---|---|---|
| `.mp4` | `.txt` | ffmpeg 抽取音频后 ASR |
| `.mp4` | `.docx`, `.doc` | 抽取音频 → ASR → Word |
| `.mp4` | `.pdf` | 抽取音频 → ASR → PDF |

视频转文本注意事项：

- 仅提取视频中的音频轨道进行识别。
- 不对视频画面内容做 OCR 分析。
- 不识别视频里的 PPT 翻页、屏幕文字、字幕图像。
- 文件大小限制 500MB。
- 多人对话可尽力标注说话人切换，但不做精确说话人分离。
- 生成文档默认带时间戳，每段开头标注 `[MM:SS]`。

---

## 7.2 格式转换

### 7.2.1 文档格式互转

| 源格式 | 目标格式 | MVP 处理方式 | 质量说明 |
|---|---|---|---|
| `.pdf` | `.docx` | OCR / 文本提取后生成 Word | 复杂排版 best-effort |
| `.pdf` | `.pptx` | 文本提取后按页/标题生成 PPT | 适合文字型 PDF |
| `.pdf` | `.xlsx`, `.xls` | 提取表格 | 仅表格数据有效 |
| `.pdf` | `.txt` | 提取纯文本 | 丢弃排版和图片 |
| `.docx`, `.doc` | `.pdf` | LibreOffice 转换 | 尽量保持原样 |
| `.docx`, `.doc` | `.pptx`, `.ppt` | 按标题层级生成 PPT | 正文可放备注 |
| `.docx`, `.doc` | `.txt` | 提取纯文本 | 丢弃格式 |
| `.pptx`, `.ppt` | `.pdf` | LibreOffice 转换 | 尽量保持原样 |
| `.pptx`, `.ppt` | `.docx`, `.doc` | 每页标题 + 正文转 Word | 丢失复杂版式 |
| `.pptx`, `.ppt` | `.txt` | 提取纯文本 | 丢弃版式和图片 |
| `.xlsx`, `.xls` | `.pdf` | LibreOffice 转换 | 尽量保持原样 |
| `.xlsx`, `.xls` | `.txt` | Tab/逗号分隔 | 丢弃格式 |
| `.xlsx`, `.xls` | `.csv` | 导出第一个工作表 | 仅第一个 sheet |
| `.txt` | `.pdf` | 默认字体和排版生成 PDF | 基础排版 |
| `.txt` | `.docx`, `.doc` | 默认字体生成 Word | 基础排版 |
| `.txt` | `.pptx`, `.ppt` | 按空行分页生成 PPT | 简单模板 |
| `.txt` | `.xlsx`, `.xls` | 按 Tab/逗号分隔拆分列 | 简单结构化 |

文档转换注意事项：

- PDF 包含扫描图片页时，自动触发 OCR。
- 扫描 PDF 转换耗时更长。
- 加密 / 密码保护 PDF 无法转换。
- Excel → PPT 不在 MVP 中单独提供。
- 如需将数据表转为 PPT 图表，后续应进入 PPT AI 生成模块。

---

### 7.2.2 图片格式互转

| 源格式 | 可转换目标格式 | 说明 |
|---|---|---|
| `.jpg`, `.jpeg` | `.png` | JPEG 转 PNG，保留画质 |
| `.jpg`, `.jpeg` | `.bmp` | JPEG 转 BMP，不推荐，文件会变大 |
| `.png` | `.jpg`, `.jpeg` | PNG 转 JPEG，透明通道变白底 |
| `.png` | `.bmp` | PNG 转 BMP |
| `.bmp` | `.jpg`, `.jpeg` | BMP 转 JPEG，文件明显变小 |
| `.bmp` | `.png` | BMP 转 PNG |

---

### 7.2.3 音频格式互转

| 源格式 | 可转换目标格式 | 说明 |
|---|---|---|
| `.mp3` | `.wav` | MP3 转 WAV，文件变大 |
| `.wav` | `.mp3` | WAV 转 MP3，有损压缩，文件变小 |
| `.m4a` | `.mp3` | M4A 转 MP3，兼容性更好 |
| `.m4a` | `.wav` | M4A 转 WAV |
| `.aac` | `.mp3` | AAC 转 MP3，兼容性更好 |
| `.aac` | `.wav` | AAC 转 WAV |

---

## 8. 产品页面设计

## 8.1 首页

页面模块：

1. 顶部标题
2. 上传区域
3. 支持格式说明
4. 转换目标选择区
5. 任务状态区
6. 最近任务列表

页面标题：

```text
多模态文件转换
```

副标题：

```text
支持文档、图片、音频、视频的格式转换与智能内容提取
```

---

## 8.2 上传区域

能力：

- 支持拖拽上传
- 支持点击选择文件
- 上传后展示文件信息
- 上传失败展示明确错误

上传后展示字段：

- 文件名
- 文件大小
- 源格式
- MIME type
- 可转换目标格式

错误场景：

- 文件格式不支持
- 文件大小超限
- 文件为空
- 上传失败
- 网络异常

---

## 8.3 转换配置区

上传成功后展示。

字段：

- 源文件信息
- 目标格式按钮
- 高级选项
- 开始转换按钮

目标格式按钮必须由后端接口返回，不允许前端硬编码。

---

## 8.4 高级选项

### OCR 选项

适用于图片 / 扫描 PDF：

| 字段 | 可选值 | 默认值 |
|---|---|---|
| OCR 语言 | auto / zh / en | auto |
| 是否保留版面 | true / false | false |
| 是否识别表格 | true / false | true |

### ASR 选项

适用于音频 / 视频：

| 字段 | 可选值 | 默认值 |
|---|---|---|
| 是否生成时间戳 | true / false | true |
| 输出语言 | auto / zh / en | auto |
| 是否尝试标注说话人 | true / false | false |

### 通用选项

| 字段 | 可选值 | 默认值 |
|---|---|---|
| 输出文件名 | string | 原文件名 + 目标后缀 |
| 失败是否降级输出 txt | true / false | true |

---

## 8.5 任务状态区

任务状态：

| 状态 | 含义 |
|---|---|
| `queued` | 排队中 |
| `running` | 转换中 |
| `succeeded` | 转换成功 |
| `failed` | 转换失败 |
| `expired` | 文件已过期 |

前端轮询规则：

- `queued` / `running`：每 2 秒查询一次
- `succeeded` / `failed` / `expired`：停止轮询

状态展示示例：

```text
正在识别音频内容...
转换进度：45%
```

---

## 8.6 下载结果区

转换成功后展示：

- 输出文件名
- 输出格式
- 文件大小
- 质量提示
- 下载按钮

质量提示示例：

```text
PDF 转 Word 为 best-effort 转换，复杂排版可能无法完全还原。
```

```text
视频转文本仅识别音频轨道，不包含画面 OCR。
```

```text
图片转 Excel 仅在检测到表格结构时效果较好。
```

---

## 9. API 设计

## 9.1 上传文件

### `POST /api/files`

请求：

```text
multipart/form-data
file: binary
```

响应：

```json
{
  "file_id": "file_xxx",
  "filename": "demo.pdf",
  "source_ext": ".pdf",
  "mime_type": "application/pdf",
  "size_bytes": 123456,
  "allowed_targets": [".docx", ".pptx", ".xlsx", ".txt"],
  "created_at": "2026-06-29T12:00:00Z"
}
```

失败响应：

```json
{
  "error": {
    "code": "FILE_TOO_LARGE",
    "message": "文件超过 50MB 限制"
  }
}
```

---

## 9.2 查询可转换目标

### `GET /api/conversions/supported?source_ext=.pdf`

响应：

```json
{
  "source_ext": ".pdf",
  "targets": [
    {
      "target_ext": ".docx",
      "handler_key": "pdf_to_docx",
      "quality": "best_effort"
    },
    {
      "target_ext": ".txt",
      "handler_key": "pdf_to_txt",
      "quality": "high"
    }
  ]
}
```

---

## 9.3 创建转换任务

### `POST /api/jobs`

请求：

```json
{
  "file_id": "file_xxx",
  "target_ext": ".docx",
  "options": {
    "ocr_language": "auto",
    "preserve_layout": false,
    "detect_tables": true,
    "timestamps": true,
    "speaker_labels": false,
    "fallback_to_txt": true
  }
}
```

响应：

```json
{
  "job_id": "job_xxx",
  "status": "queued"
}
```

---

## 9.4 查询任务状态

### `GET /api/jobs/{job_id}`

运行中响应：

```json
{
  "job_id": "job_xxx",
  "status": "running",
  "progress": 45,
  "message": "正在识别音频内容",
  "source_file": {
    "filename": "meeting.mp4",
    "source_ext": ".mp4"
  },
  "target_ext": ".docx",
  "result": null,
  "error": null
}
```

成功响应：

```json
{
  "job_id": "job_xxx",
  "status": "succeeded",
  "progress": 100,
  "message": "转换完成",
  "target_ext": ".docx",
  "result": {
    "download_url": "/api/jobs/job_xxx/download",
    "filename": "meeting.docx",
    "size_bytes": 456789,
    "quality_notice": "视频转文本仅识别音频轨道，不包含画面 OCR。"
  },
  "error": null
}
```

失败响应：

```json
{
  "job_id": "job_xxx",
  "status": "failed",
  "progress": 100,
  "message": "转换失败",
  "result": null,
  "error": {
    "code": "UNSUPPORTED_CONVERSION",
    "message": "不支持 .mp4 转 .xlsx"
  }
}
```

---

## 9.5 下载结果

### `GET /api/jobs/{job_id}/download`

返回文件流。

要求：

- 如果任务未完成，返回 `DOWNLOAD_NOT_READY`
- 如果文件过期，返回 `FILE_EXPIRED`
- 如果任务失败，返回错误信息
- 下载文件名应使用安全处理后的文件名

---

## 10. 数据模型

## 10.1 FileRecord

```ts
type FileRecord = {
  id: string
  originalFilename: string
  storedPath: string
  sourceExt: string
  mimeType: string
  sizeBytes: number
  status: "uploaded" | "deleted" | "expired"
  createdAt: string
  expiresAt: string
}
```

---

## 10.2 ConversionJob

```ts
type ConversionJob = {
  id: string
  fileId: string
  sourceExt: string
  targetExt: string
  handlerKey: string
  status: "queued" | "running" | "succeeded" | "failed" | "expired"
  progress: number
  message: string | null
  options: Record<string, unknown>
  resultPath: string | null
  resultFilename: string | null
  resultSizeBytes: number | null
  errorCode: string | null
  errorMessage: string | null
  createdAt: string
  updatedAt: string
  finishedAt: string | null
}
```

---

## 11. Conversion Registry 设计

后端必须实现统一注册表。

示例：

```python
CONVERSION_REGISTRY = {
    ".pdf": [
        {"target": ".docx", "handler": "pdf_to_docx", "quality": "best_effort"},
        {"target": ".pptx", "handler": "pdf_to_pptx", "quality": "best_effort"},
        {"target": ".xlsx", "handler": "pdf_to_xlsx", "quality": "table_only"},
        {"target": ".txt", "handler": "pdf_to_txt", "quality": "high"},
    ],
    ".jpg": [
        {"target": ".txt", "handler": "image_ocr_to_txt", "quality": "high"},
        {"target": ".docx", "handler": "image_ocr_to_docx", "quality": "medium"},
        {"target": ".xlsx", "handler": "image_ocr_to_xlsx", "quality": "table_only"},
        {"target": ".pdf", "handler": "image_ocr_to_pdf", "quality": "medium"},
        {"target": ".pptx", "handler": "image_ocr_to_pptx", "quality": "best_effort"},
        {"target": ".png", "handler": "image_format_convert", "quality": "high"},
        {"target": ".bmp", "handler": "image_format_convert", "quality": "high"},
    ],
    ".mp3": [
        {"target": ".txt", "handler": "audio_asr_to_txt", "quality": "high"},
        {"target": ".docx", "handler": "audio_asr_to_docx", "quality": "high"},
        {"target": ".pdf", "handler": "audio_asr_to_pdf", "quality": "high"},
        {"target": ".wav", "handler": "audio_format_convert", "quality": "high"},
    ],
    ".mp4": [
        {"target": ".txt", "handler": "video_asr_to_txt", "quality": "high"},
        {"target": ".docx", "handler": "video_asr_to_docx", "quality": "high"},
        {"target": ".pdf", "handler": "video_asr_to_pdf", "quality": "high"},
    ],
}
```

要求：

- registry 必须由后端维护。
- 前端通过接口获取可转换目标。
- 不支持的转换必须在后端拒绝。
- 新增转换路线只允许通过扩展 registry 和 handler 实现。

---

## 12. Handler 设计

每个转换 handler 必须实现统一接口。

```python
from abc import ABC, abstractmethod
from typing import Any

class ConversionHandler(ABC):
    key: str

    @abstractmethod
    def supports(self, source_ext: str, target_ext: str) -> bool:
        pass

    @abstractmethod
    async def run(
        self,
        input_path: str,
        output_dir: str,
        target_ext: str,
        options: dict[str, Any],
    ) -> dict[str, Any]:
        pass
```

返回结构：

```python
{
    "output_path": "/storage/results/job_xxx.docx",
    "filename": "result.docx",
    "size_bytes": 123456,
    "quality_notice": "PDF 转 Word 为 best-effort 转换，复杂排版可能无法完全还原。"
}
```

---

## 13. Handler 列表

## 13.1 P0：基础能力

必须优先实现：

- `txt_to_docx`
- `txt_to_pdf`
- `txt_to_pptx`
- `txt_to_xlsx`
- `image_format_convert`
- `audio_format_convert`
- `office_to_pdf`
- `office_to_txt`

---

## 13.2 P1：多模态核心能力

第二阶段实现：

- `image_ocr_to_txt`
- `image_ocr_to_docx`
- `image_ocr_to_pdf`
- `audio_asr_to_txt`
- `audio_asr_to_docx`
- `audio_asr_to_pdf`
- `video_asr_to_txt`
- `video_asr_to_docx`
- `video_asr_to_pdf`

---

## 13.3 P2：复杂文档互转

第三阶段实现：

- `pdf_to_docx`
- `pdf_to_pptx`
- `pdf_to_xlsx`
- `docx_to_pptx`
- `pptx_to_docx`
- `xlsx_to_csv`

---

## 14. OCR Provider 设计

## 14.1 Provider 接口

```python
class OCRProvider:
    async def extract_text(
        self,
        image_path: str,
        language: str = "auto",
        detect_tables: bool = True,
        preserve_layout: bool = False,
    ) -> dict:
        pass
```

返回：

```json
{
  "text": "识别出的文本",
  "tables": [
    {
      "rows": [
        ["姓名", "金额"],
        ["张三", "100"]
      ]
    }
  ],
  "confidence": 0.92
}
```

## 14.2 MVP Provider 选择

可选方案：

1. 本地 OCR：PaddleOCR
2. 云 OCR：火山 / 阿里云 / 腾讯云 / OpenAI-compatible 多模态模型
3. 混合：本地优先，失败后云 OCR

MVP 建议：

- 先定义 Provider 接口。
- 第一版可接一个具体 OCR 服务。
- 不要把 OCR 调用散落在各个 handler 里。

---

## 15. ASR Provider 设计

## 15.1 Provider 接口

```python
class ASRProvider:
    async def transcribe(
        self,
        audio_path: str,
        language: str = "auto",
        timestamps: bool = True,
        speaker_labels: bool = False,
    ) -> dict:
        pass
```

返回：

```json
{
  "text": "完整转写文本",
  "segments": [
    {
      "start": 0.0,
      "end": 4.2,
      "text": "大家好，今天我们讨论项目进展。"
    }
  ],
  "language": "zh",
  "duration_seconds": 128.5
}
```

## 15.2 视频转文本流程

```text
上传 mp4
  ↓
ffmpeg 提取音频
  ↓
ASR Provider 转写
  ↓
生成 txt / docx / pdf
  ↓
返回下载结果
```

---

## 16. 错误码

| 错误码 | 场景 | 用户提示 |
|---|---|---|
| `UNSUPPORTED_FILE_TYPE` | 文件格式不支持 | 当前文件格式暂不支持 |
| `FILE_TOO_LARGE` | 文件超过限制 | 文件超过大小限制 |
| `EMPTY_FILE` | 文件为空 | 文件为空，请重新上传 |
| `UNSUPPORTED_CONVERSION` | 不支持该转换路线 | 当前源格式不能转换为该目标格式 |
| `PASSWORD_PROTECTED_PDF` | PDF 加密 | PDF 已加密，请解除密码后重新上传 |
| `OCR_FAILED` | OCR 失败 | 图片文字识别失败，请更换清晰图片 |
| `ASR_FAILED` | ASR 失败 | 音频识别失败，请检查音频质量 |
| `NO_AUDIO_TRACK` | 视频无音轨 | 视频中未检测到音频轨道 |
| `NO_TABLE_FOUND` | 未识别到表格 | 未识别到可导出的表格 |
| `CONVERSION_TIMEOUT` | 转换超时 | 转换超时，请尝试较小文件 |
| `CONVERSION_ENGINE_ERROR` | 转换引擎异常 | 转换服务异常，请稍后重试 |
| `FILE_EXPIRED` | 文件已过期 | 文件已过期，请重新上传 |
| `DOWNLOAD_NOT_READY` | 结果未生成 | 转换尚未完成，请稍后下载 |

---

## 17. 文件安全与隐私

必须实现：

1. 上传文件名 sanitize。
2. 文件实际存储名使用 UUID，不直接使用用户原始文件名。
3. 每个 job 使用独立 workdir。
4. 禁止路径穿越。
5. 限制文件大小。
6. 校验扩展名和 MIME type。
7. 不执行 Office 宏。
8. 加密 PDF 直接拒绝。
9. 转换完成后文件默认保留 24 小时。
10. 定时清理过期文件。
11. 日志不打印完整文件内容。
12. 错误返回不暴露服务器路径。
13. 下载接口只能下载当前 job 的 result 文件。
14. 临时文件转换完成后清理。

---

## 18. 任务超时

| 类型 | 建议超时 |
|---|---:|
| 图片格式转换 | 60 秒 |
| 图片 OCR | 180 秒 |
| 文档格式转换 | 180 秒 |
| 音频转文本 | 10 分钟 |
| 视频转文本 | 20 分钟 |
| 大 PDF OCR | 20 分钟 |

要求：

- API 不能同步阻塞等待长任务完成。
- 所有转换任务必须异步执行。
- 前端通过轮询查询状态。
- 超时后任务状态改为 `failed`，错误码为 `CONVERSION_TIMEOUT`。

---

## 19. 推荐目录结构

```text
multimodal-converter/
  apps/
    web/
      app/
      components/
      lib/
      types/
    api/
      main.py
      routers/
        files.py
        jobs.py
        conversions.py
      services/
        storage.py
        registry.py
        job_runner.py
      handlers/
        base.py
        txt_handlers.py
        image_handlers.py
        audio_handlers.py
        video_handlers.py
        office_handlers.py
        pdf_handlers.py
      providers/
        ocr_provider.py
        asr_provider.py
      models/
      tests/
  storage/
    uploads/
    results/
    workdirs/
  docs/
    SPEC.md
  docker-compose.yml
  README.md
```

---

## 20. 环境变量

```env
APP_ENV=development
API_BASE_URL=http://localhost:8000

STORAGE_ROOT=./storage
FILE_RETENTION_HOURS=24

MAX_IMAGE_MB=20
MAX_PDF_MB=50
MAX_WORD_MB=30
MAX_PPT_MB=50
MAX_EXCEL_MB=30
MAX_TXT_MB=10
MAX_AUDIO_MB=100
MAX_WAV_MB=200
MAX_VIDEO_MB=500

OCR_PROVIDER=local
OCR_API_KEY=

ASR_PROVIDER=local
ASR_API_KEY=

LIBREOFFICE_BIN=libreoffice
FFMPEG_BIN=ffmpeg
```

---

## 21. 开发顺序

## 21.1 Phase 1：项目骨架

目标：

跑通上传、转换任务、状态查询、下载的完整链路。

任务：

1. 创建 monorepo 项目结构。
2. 创建 Next.js 前端。
3. 创建 FastAPI 后端。
4. 实现上传接口。
5. 实现文件类型和大小校验。
6. 实现 conversion registry。
7. 实现查询可转换目标接口。
8. 实现任务模型。
9. 实现创建任务接口。
10. 实现任务状态查询接口。
11. 实现下载接口。
12. 前端实现上传、目标选择、创建任务、轮询、下载。

验收：

- 上传文件成功。
- 非法格式被拒绝。
- 超大文件被拒绝。
- 上传后能展示合法目标格式。
- 创建任务后能看到状态变化。
- mock handler 不允许作为最终交付，但 Phase 1 可用临时 handler 验证链路，Phase 2 必须替换为真实转换。

---

## 21.2 Phase 2：基础格式转换

目标：

实现不依赖 AI 的真实转换能力。

任务：

1. 实现 `txt_to_docx`
2. 实现 `txt_to_pdf`
3. 实现 `txt_to_pptx`
4. 实现 `txt_to_xlsx`
5. 实现 `image_format_convert`
6. 实现 `audio_format_convert`
7. 实现 `office_to_pdf`
8. 实现 `office_to_txt`
9. 实现 `xlsx_to_csv`
10. 编写真实样本测试

验收转换：

- `.txt → .docx`
- `.txt → .pdf`
- `.txt → .pptx`
- `.txt → .xlsx`
- `.jpg → .png`
- `.png → .jpg`
- `.bmp → .jpg`
- `.mp3 → .wav`
- `.wav → .mp3`
- `.m4a → .mp3`
- `.aac → .mp3`
- `.docx → .pdf`
- `.pptx → .pdf`
- `.xlsx → .csv`

---

## 21.3 Phase 3：多模态转换

目标：

接入 OCR / ASR，实现图片、音频、视频转文本类文档。

任务：

1. 实现 OCR Provider 接口。
2. 接入一个 OCR 实现。
3. 实现图片 OCR → txt。
4. 实现图片 OCR → docx。
5. 实现图片 OCR → pdf。
6. 实现 ASR Provider 接口。
7. 接入一个 ASR 实现。
8. 实现音频 → txt。
9. 实现音频 → docx。
10. 实现音频 → pdf。
11. 实现视频抽音频。
12. 实现视频 → txt。
13. 实现视频 → docx。
14. 实现视频 → pdf。
15. 加入时间戳输出。

验收转换：

- `.jpg → .txt`
- `.jpg → .docx`
- `.png → .pdf`
- `.mp3 → .txt`
- `.mp3 → .docx`
- `.wav → .pdf`
- `.mp4 → .txt`
- `.mp4 → .docx`
- `.mp4 → .pdf`

---

## 21.4 Phase 4：复杂文档互转

目标：

补齐复杂文档互转能力。

任务：

1. 实现 `pdf_to_txt`
2. 实现 `pdf_to_docx`
3. 实现 `pdf_to_xlsx`
4. 实现 `pdf_to_pptx`
5. 实现 `docx_to_pptx`
6. 实现 `pptx_to_docx`
7. 补充质量提示
8. 补充复杂文档测试

验收转换：

- `.pdf → .txt`
- `.pdf → .docx`
- `.pdf → .xlsx`
- `.pdf → .pptx`
- `.docx → .pptx`
- `.pptx → .docx`

---

## 22. 自动化测试要求

至少覆盖：

### 22.1 Registry 测试

- 支持格式能返回目标格式。
- 不支持格式返回错误。
- 不支持转换路线返回错误。
- registry 中每个 handler_key 都有对应 handler。

### 22.2 文件校验测试

- 支持格式上传成功。
- 不支持格式上传失败。
- 超大文件上传失败。
- 空文件上传失败。
- MIME type 与扩展名不一致时拒绝或降级为高风险处理。

### 22.3 转换任务测试

- 创建任务成功。
- 非法目标格式创建任务失败。
- 任务状态能从 queued → running → succeeded。
- 失败任务能返回结构化错误。
- 下载未完成任务返回 `DOWNLOAD_NOT_READY`。

### 22.4 真实转换测试

必须使用真实样本文件验证：

- 输出文件存在。
- 输出文件大小大于 0。
- 输出文件后缀正确。
- 输出文件可以被对应库打开。
- 不接受 fake download。
- 不接受 mock result。

---

## 23. 测试样本要求

准备以下样本：

```text
samples/
  sample.txt
  sample.docx
  sample.pdf
  sample.jpg
  sample.png
  sample.bmp
  sample.mp3
  sample.wav
  sample.m4a
  sample.aac
  sample.mp4
  sample.xlsx
  sample.pptx
```

样本要求：

- `sample.txt`：包含中英文、多段落、逗号和 Tab 数据。
- `sample.docx`：包含标题、正文、简单表格。
- `sample.pdf`：包含文字型 PDF 和扫描型 PDF 各一个更好。
- `sample.jpg/png`：包含清晰文字和简单表格。
- `sample.mp3/wav`：包含 10-30 秒中文语音。
- `sample.mp4`：包含音轨，时长 10-30 秒。
- `sample.xlsx`：至少两个 sheet，但导出 csv 只处理第一个 sheet。
- `sample.pptx`：至少 3 页，每页有标题和正文。

---

## 24. 验收标准

## 24.1 功能验收

- 用户可以上传文件。
- 用户可以看到源文件信息。
- 用户只能选择合法目标格式。
- 用户可以创建转换任务。
- 用户可以看到转换进度。
- 用户可以下载转换结果。
- 用户可以看到失败原因。
- 文件过期后不能下载。
- 最近任务列表能展示任务记录。

## 24.2 质量验收

- 不支持格式不会进入转换流程。
- 超大文件不会进入转换流程。
- 不支持转换不会创建任务。
- 后端不会相信前端传入的 handler_key。
- 所有转换都通过后端 registry 决定。
- 转换失败不会导致服务崩溃。
- 下载接口不会暴露服务器路径。
- 每个 job 的临时文件相互隔离。

## 24.3 工程验收

- README 完整。
- `.env.example` 完整。
- API 有基础文档。
- 测试可以一键运行。
- 本地可以通过 docker-compose 启动。
- 关键 handler 有单元测试。
- 关键接口有集成测试。

---

## 25. README 必须包含

README 至少包括：

1. 项目简介
2. 功能列表
3. 技术栈
4. 本地启动方式
5. 环境变量说明
6. 依赖安装说明
7. LibreOffice 安装说明
8. ffmpeg 安装说明
9. OCR Provider 配置说明
10. ASR Provider 配置说明
11. 测试样本说明
12. 运行测试方式
13. 常见错误排查

---

## 26. Docker Compose 要求

至少包含：

```yaml
services:
  web:
    build: ./apps/web
    ports:
      - "3000:3000"
    environment:
      - API_BASE_URL=http://api:8000

  api:
    build: ./apps/api
    ports:
      - "8000:8000"
    volumes:
      - ./storage:/app/storage
    environment:
      - STORAGE_ROOT=/app/storage
```

如果使用 Redis / Celery，增加：

```yaml
  redis:
    image: redis:7

  worker:
    build: ./apps/api
    command: celery -A worker worker --loglevel=info
    depends_on:
      - redis
```

MVP 可以先不用 worker，但代码结构要能迁移。

---

## 27. 给 Coding Agent 的启动提示词

```text
你是资深 full-stack engineer。请基于 docs/SPEC.md 开发一个多模态文件转换 Web 应用。

技术栈：
- 前端：Next.js + React + TypeScript + Tailwind + shadcn/ui
- 后端：FastAPI + Python
- 存储：MVP 使用本地 storage 目录
- 任务：MVP 可先用 BackgroundTasks，但代码结构要能后续替换为 Redis/RQ 或 Celery
- 转换：必须实现真实文件转换，不允许 mock download，不允许 fake result

开发要求：
1. 先实现项目骨架、上传、conversion registry、任务创建、状态轮询、下载。
2. 后端 registry 是最终校验源，前端 registry 只能用于展示。
3. 每个转换 handler 必须走统一接口。
4. 所有上传文件必须校验扩展名、MIME type、大小限制。
5. 每个 job 使用独立 workdir。
6. 转换失败必须返回结构化 error code 和用户可读 message。
7. 首轮至少完成以下真实转换：
   - txt -> docx
   - txt -> pdf
   - txt -> pptx
   - txt -> xlsx
   - jpg/png/bmp 互转
   - mp3/wav/m4a/aac 互转
   - docx/pptx/xlsx -> pdf
   - docx/pptx/xlsx -> txt
   - xlsx -> csv
8. 第二轮接入 OCR/ASR Provider，实现图片/音频/视频转文本类文档。
9. 给出 README，包含启动方式、环境变量、测试样本说明。
10. 写自动化测试，至少覆盖 registry 校验、文件大小校验、不支持转换拒绝、真实转换输出文件存在且可打开。

请先生成实施计划，然后按 Phase 1 → Phase 2 → Phase 3 → Phase 4 的顺序开发。每完成一个 phase，运行测试并输出验收结果。
```

---

## 28. 风险与注意事项

### 28.1 PDF 转 Word / PPT 风险

PDF 转 Word / PPT 很难做到 100% 原样还原。

MVP 必须标注：

```text
该转换为 best-effort，复杂排版可能无法完全还原。
```

### 28.2 图片转 Excel 风险

图片转 Excel 依赖表格识别。

如果没有识别到表格，应返回：

```json
{
  "code": "NO_TABLE_FOUND",
  "message": "未识别到可导出的表格"
}
```

### 28.3 视频转文本风险

视频转文本只处理音轨。

必须在 UI 上标注：

```text
仅识别视频中的音频内容，不分析视频画面。
```

### 28.4 大文件风险

音频、视频、大 PDF 都可能耗时很长。

必须异步处理，不能让 HTTP 请求一直等待。

### 28.5 依赖环境风险

LibreOffice 和 ffmpeg 是系统级依赖。

必须在 README 和 Dockerfile 中明确安装。

---

## 29. 最小可交付版本定义

最小可交付版本不要求完成全部矩阵，但必须满足：

1. Web 页面可用。
2. 上传真实文件可用。
3. 后端能校验格式和大小。
4. 能展示合法目标格式。
5. 能创建异步任务。
6. 能查询任务状态。
7. 能下载真实转换结果。
8. 至少完成以下真实转换：
   - `.txt → .docx`
   - `.txt → .pdf`
   - `.txt → .pptx`
   - `.txt → .xlsx`
   - `.jpg → .png`
   - `.png → .jpg`
   - `.mp3 → .wav`
   - `.wav → .mp3`
   - `.docx → .pdf`
   - `.pptx → .pdf`
   - `.xlsx → .csv`
9. 所有 mock handler 必须移除。
10. README 和测试说明完整。

---

## 30. 后续扩展方向

后续可以扩展：

- 用户登录
- 转换历史
- 批量转换
- 云存储
- 队列 worker
- 文件分享链接
- API Key 接入
- 付费套餐
- OCR 高精度模式
- ASR 高精度模式
- 视频字幕生成
- PPT AI 生成模块
- Excel 数据生成图表 PPT
- 企业私有化部署
- 转换质量评分
- 多 Provider 路由
- 失败自动降级策略

---

# 结论

该需求做成 Web 应用完全可行。

最快开发路径：

1. 先做统一上传、registry、任务、下载链路。
2. 再实现不依赖 AI 的基础格式转换。
3. 然后接入 OCR / ASR。
4. 最后补复杂 PDF / Office 互转能力。

不要一开始追求完整全矩阵和完美排版还原，否则很容易开发周期失控。
