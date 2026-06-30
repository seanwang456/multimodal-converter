# 多模态文件转换 Web 应用

上传文件 → 自动识别源格式 → 选择目标格式 → 异步转换 → 下载结果。
覆盖办公文档、图片、音视频等格式互转，以及 OCR / ASR（音视频转文字）能力。

## 功能

- **办公文档互转**：Word / Excel / PPT / PDF 之间互转（`.docx/.xlsx/.pptx/.pdf`；旧版 `.doc/.ppt/.xls` → `.pdf`）
- **图片**：`jpg/png/bmp` 互转；图片 OCR → 文字 / Word / PDF
- **音频**：`mp3/wav/m4a/aac` 互转；音频转文字（ASR）→ 文字 / Word / PDF
- **视频**：`mp4` → 文字 / Word / PDF（识别音轨，不含画面 OCR）
- **其他**：`txt → docx/pdf/pptx/xlsx`、`pdf → txt/docx/pptx`、`xlsx → csv`

转换走异步任务、支持并发；结果默认保留 24 小时。需求规格见 [`multimodal_converter_SPEC.md`](./multimodal_converter_SPEC.md)。

## 技术栈

- 前端：Next.js + TypeScript + Tailwind（`apps/web`）
- 后端：FastAPI + SQLModel(SQLite) + Redis 队列 + 独立 worker（`apps/api`）
- 转换引擎：LibreOffice headless / ffmpeg / Pillow / python-docx / openpyxl / python-pptx / reportlab / pdfplumber
- 多模态：OCR / ASR 走可插拔 provider（OpenAI 兼容 / 火山豆包录音文件识别）
- 部署：docker-compose（web / api / redis / worker / caddy）

## 部署（Docker 一键）

镜像已内置 LibreOffice、ffmpeg、中文字体——**服务器只需装 Docker，无需任何系统依赖**。

```bash
git clone <仓库地址> && cd multimodal-converter
cp .env.example .env
vi .env                # 填域名与 LLM/ASR key（见下表）
docker compose up -d --build
docker compose up -d --wait      # 退出码 0 = 全部健康
```

访问 `https://<域名>`（首次自动申请 HTTPS 证书）。
完整运维交接文档（拓扑 / 健康检查 / 备份 / 排错 / Checklist）见 [`DEPLOY.md`](./DEPLOY.md)。

### 必填环境变量（`.env`）

| 变量 | 说明 |
|---|---|
| `CADDY_DOMAIN` | 正式域名，驱动自动 HTTPS（留空=仅本地反代） |
| `CORS_ORIGINS` | 前端来源，一般填 `https://<域名>` |
| `LLM_BASE_URL` / `LLM_API_KEY` | OCR 用的 OpenAI 兼容端点与密钥 |
| `LLM_VISION_MODEL` | 视觉模型名（如 `doubao-seed-2-0-mini-260428`） |
| `ASR_PROVIDER` | ASR 后端：`volcano`（火山）或 `openai`（Whisper 兼容） |
| `VOLCANO_ASR_API_KEY` | 火山语音 APP Key（`ASR_PROVIDER=volcano` 时必填） |
| `PUBLIC_BASE_URL` | 火山 ASR 回拉音频的公网地址（=域名，火山模式必填） |

**更换服务商**：OCR 与 Whisper 兼容 ASR 仅改 `LLM_*` / `ASR_PROVIDER` 即可；私有协议 ASR 新增一个 provider 文件即可（见 `apps/api/app/providers/`）。

> ⚠️ `.env` 含密钥，已被 `.gitignore` 忽略，**切勿提交**；正式环境请使用公司自己的 key。

## 本地开发

```bash
# 后端
cd apps/api && uv venv && uv pip install -e ".[dev,convert]"
uv run pytest -q                              # 跑测试（Inline runner，无需 Redis）
uv run uvicorn app.main:app --reload --port 8000

# 前端
cd apps/web && pnpm install && pnpm dev       # http://localhost:3000
```

本地**原生**开发（不走 Docker）时，若要验证 Office / 音视频转换，需自行安装 LibreOffice 与 ffmpeg。

## 目录结构

```
apps/
  web/            Next.js 前端
  api/            FastAPI 后端
    app/
      routers/      files / conversions / jobs / asr_source
      services/     storage / registry / job_runner
      handlers/     各类转换处理器
      providers/    OCR / ASR provider（可插拔）
    tests/
docker-compose.yml / Caddyfile / .env.example
DEPLOY.md                          运维部署交接文档
multimodal_converter_SPEC.md       需求规格（事实来源）
```
