# 部署交接文档（运维）

> 多模态文件转换 Web 应用 · 单机一键部署（docker-compose）

## 1. 服务拓扑（5 个容器）

| 服务 | 镜像 | 端口 | 作用 |
|------|------|------|------|
| `web` | 自建（Next.js） | 3000 | 前端 UI |
| `api` | 自建（FastAPI，含 LibreOffice + ffmpeg + CJK 字体） | 8000 | 后端 API（上传/任务/下载） |
| `worker` | 同 `api` 镜像 | — | 异步转换执行（消费 Redis 队列） |
| `redis` | redis:7-alpine | 6379 | 任务队列 |
| `caddy` | caddy:2-alpine | 80/443 | 反向代理 + 自动 HTTPS |

外部入口：**只暴露 Caddy 的 80/443**；`web`/`api`/`redis` 不必对公网开放（compose 内网互通）。

## 2. 服务器前置条件

- Docker ≥ 24 + Docker Compose v2（`docker compose` 可用）
- 单机建议：≥ 2 vCPU / 4G 内存 / 40G 磁盘（LibreOffice 镜像约 1.5G，转换有临时文件）
- 一个域名（如 `convert.example.com`）指向服务器公网 IP，80/443 可达（用于 Caddy 自动签 TLS）
- OCR 可用的 LLM 凭证（OpenAI-compatible）：`LLM_BASE_URL` + `LLM_API_KEY` + 视觉模型名
  - 已验证：火山方舟 Doubao（`https://ark.cn-beijing.volces.com/api/v3`，模型 `doubao-seed-2-0-mini-260428`，走 chat/completions vision）
  - ASR（音频/视频→文字）默认走火山豆包「录音文件识别」（`ASR_PROVIDER=volcano`）：需 `VOLCANO_ASR_API_KEY` + **公网可达的 `PUBLIC_BASE_URL`**（火山从该地址回拉音频，`localhost` 不可用；通常 `PUBLIC_BASE_URL=https://<CADDY_DOMAIN>`）。Doubao chat 端点本身无音频转写能力。

## 3. 一键部署

```bash
# 1) 拉代码
git clone <仓库地址> converter && cd converter

# 2) 生成配置（从模板拷贝后填写真实凭证）
cp .env.example .env
vi .env   # 重点填：LLM_BASE_URL / LLM_API_KEY / LLM_VISION_MODEL / CADDY_DOMAIN / CORS_ORIGINS

# 3) 构建并启动（首次会构建 web/api 镜像，约 5-15 分钟）
docker compose up -d --build

# 4) 跟随启动直到健康
docker compose up -d --wait   # 退出码 0 即全部健康
```

访问：`https://<域名>`（CADDY_DOMAIN）。首次会自动申请 Let's Encrypt 证书，约 10-30 秒。

## 4. 必填环境变量（`.env`）

| 变量 | 必填 | 说明 |
|------|------|------|
| `LLM_BASE_URL` | ✅ | OCR 用的 OpenAI-compatible 端点 |
| `LLM_API_KEY` | ✅ | 对应密钥（**严禁提交进 git，注意轮换**） |
| `LLM_VISION_MODEL` | ✅ | 视觉模型名（如 `qwen3.6-flash`、`doubao-seed-2-0-mini-260428`） |
| `ASR_PROVIDER` | 可选 | ASR 后端：`aliyun`（推荐）/ `volcano` / `openai`（Whisper 兼容） |
| `ALIYUN_ASR_MODEL` | aliyun 时 | 默认 `qwen3-asr-flash-filetrans`；key/host 留空即复用 `LLM_API_KEY`/`LLM_BASE_URL` |
| `VOLCANO_ASR_API_KEY` | volcano 时 | 火山控制台 APP Key（**注意轮换**） |
| `PUBLIC_BASE_URL` | aliyun/volcano 必填 | 公网可达基址（一般填 `https://<CADDY_DOMAIN>`），服务商回拉音频用；`openai` 模式不需要 |
| `VOLCANO_ASR_RESOURCE_ID` | 可选 | `volc.bigasr.auc`（1.0，默认）/ `volc.seedasr.auc`（2.0） |
| `CADDY_DOMAIN` | ✅ | 正式域名（留空=仅本地 localhost 无 TLS） |
| `CORS_ORIGINS` | ✅ | 前端来源（同域经 Caddy 时可设为 `https://<域名>`） |
| `MAX_CONCURRENT_JOBS` | 可选 | 并发上限，默认 4 |
| `FILE_RETENTION_HOURS` | 可选 | 文件保留，默认 24 |

> 其余项见 `.env.example`，均有默认值。`.env` 已在 `.gitignore` 内，**不要提交**。

## 5. 健康检查与验证

```bash
docker compose ps                 # 所有服务应 Up / healthy
curl -fsS https://<域名>/api/health   # 期望 {"status":"ok"}
docker compose logs -f api worker # 查日志
```

功能冒烟（部署后建议跑一次）：
```bash
# 上传 txt → 转 pdf
F=$(curl -s -F "file=@samples/sample.txt" https://<域名>/api/files | python3 -c "import sys,json;print(json.load(sys.stdin)['file_id'])")
J=$(curl -s -X POST https://<域名>/api/jobs -H "Content-Type: application/json" -d "{\"file_id\":\"$F\",\"target_ext\":\".pdf\"}" | python3 -c "import sys,json;print(json.load(sys.stdin)['job_id'])")
curl -s -o /tmp/r.pdf -w "%{http_code}\n" https://<域名>/api/jobs/$J/download   # 200 即 OK
```

## 6. 数据与备份

- **SQLite**：`storage/app.db`（在 `storage` 持久卷内）——任务/文件记录
- **文件**：`storage/{uploads,results,workdirs}`——上传与转换结果（24h 自动清理）
- 备份：定期快照 `storage` 卷即可（含库 + 文件）。或：
  ```bash
  docker compose exec api tar czf - -C /app/storage . > backup-$(date +%F).tgz
  ```

## 7. 运维操作

```bash
docker compose restart api worker   # 重启后端（任务在 Redis，不丢）
docker compose logs -f --tail=200 worker
docker compose down                 # 停止（保留卷）
docker compose pull && docker compose up -d --build   # 升级版本
```

## 8. 已知限制（务必告知）

1. **ASR（音/视频→文字）**：支持三种后端（`ASR_PROVIDER`）：`aliyun`（阿里云千问3-ASR-Flash-Filetrans，推荐）/ `volcano`（火山豆包录音文件识别）/ `openai`（Whisper 兼容）。`aliyun` 与 `volcano` 均为「提交音频 URL + 轮询」模式，服务商从公网回拉音频，故**必须配置公网可达的 `PUBLIC_BASE_URL`**（部署时填 `https://<CADDY_DOMAIN>` 即可，`/api/asr-source/{token}` 已随 `/api/*` 被 Caddy 反代）；`openai` 模式直接上传字节，不需要。`PUBLIC_BASE_URL` 为空或为 `localhost` 时，回拉类 ASR 会返回 `ASR_FAILED`。注意：key 所在账号需在服务商控制台为对应模型开通授权，否则返回 `access_denied`。
2. **并发**：默认 4（`MAX_CONCURRENT_JOBS`）。LibreOffice 并发较高时可能不稳定，重 office 负载建议设为 2。
3. **单机部署**：1 worker + SQLite。要多副本横向扩展需迁移到 PostgreSQL + 多 worker（代码已抽象 JobRunner/Storage，可平滑迁移）。
4. **无限流 / 无队列上限**：公网暴露建议在 Caddy 前再加一层限流（或 WAF）。

## 9. 交接 Checklist（给运维逐项确认）

- [ ] 服务器已装 Docker + Compose v2
- [ ] 域名 DNS 已指向服务器，80/443 防火墙放行
- [ ] `.env` 已填真实 `LLM_*`、`CADDY_DOMAIN`、`CORS_ORIGINS`
- [ ] `.env` 未被提交进 git（`git status` 应忽略）
- [ ] `docker compose up -d --build` 成功，`docker compose ps` 全 healthy
- [ ] `https://<域名>/api/health` 返回 ok，证书已签发
- [ ] 已做一次 txt→pdf 冒烟，下载 200
- [ ] 已配置 `storage` 卷的定期备份
- [ ] 知晓 ASR 暂不可用、并发默认 4、单机限制

## 10. 常见排错

| 现象 | 排查 |
|------|------|
| Caddy 证书签发失败 | DNS 未生效 / 80/443 不通；查 `docker compose logs caddy` |
| 上传图片→txt 失败 `OCR_FAILED` | `LLM_*` 未配/无效；查 api 日志 |
| office→pdf 失败 `CONVERSION_ENGINE_ERROR` | 容器内 LibreOffice 异常；查 worker 日志，降低并发 |
| 音/视频→txt `ASR_FAILED` | 检查 `VOLCANO_ASR_API_KEY`、`PUBLIC_BASE_URL` 是否公网可达；火山侧查 `X-Tt-Logid` |
| 下载 `FILE_EXPIRED` | 结果超 24h，重新上传转换 |
