# 扫描型 PDF 页级并发 OCR 设计

## 目标

将扫描型及混合型 PDF 的 OCR Provider 请求从逐页串行处理改为受控并发处理。默认同一份 PDF 最多同时识别 3 个扫描页；由于 pdfplumber 渲染路径使用全局非线程安全的 PDFium，同一 API/worker 进程内的 PDFium 渲染必须全局串行。在不改变现有 API、转换结果顺序和错误码的前提下，通过重叠 OCR Provider 等待时间缩短多页扫描 PDF 转 TXT/Word 的耗时。

## 现状与问题

当前 PDF 文本提取会先检查每一页的原生文本层，再对需要 OCR 的页面逐个执行：

1. 以 250 DPI 将页面渲染为 PNG；
2. 调用 `OCRProvider.extract_text(...)`；
3. 删除临时 PNG；
4. 开始处理下一页。

任务 worker 已通过 `MAX_CONCURRENT_JOBS` 支持多个转换任务并发，但一份多页扫描 PDF 内部仍完全串行。其总耗时约等于所有扫描页渲染和 OCR 耗时之和。

## 方案选择

采用“单份 PDF 固定大小的页级 OCR 工作池 + 进程级 PDFium 渲染互斥”。每个 worker 协程重复领取一个待 OCR 页面；页面渲染在线程池中执行，但所有 `_render_pdf_page` 调用共用一个进程级 mutex。渲染退出 mutex 后，Provider 调用按单份 PDF 配置值并发，不使用全局 OCR limiter。

没有采用以下方案：

- **Worker 全局 OCR 并发池**：限流更集中，但多个用户的任务会互相占用同一组槽位，且会把本次 PDF 优化扩展成所有 OCR handler 的调度改造。
- **单次请求发送多页图片**：依赖模型的多图能力和请求大小限制，输出与页码的对应关系也更难可靠校验。

页级工作池和 PDFium 渲染互斥都只改变 PDF handler 内部实现，并继续复用现有 `OCRProvider` 接口。全局互斥是 PDFium 的进程安全边界，不是 OCR Provider 的全局限流。

## 配置

新增部署环境变量：

```dotenv
PDF_OCR_PAGE_CONCURRENCY=3
```

- 默认值为 `3`。
- 有效范围为 `1–8`；低于 1 按 1 处理，高于 8 按 8 处理，非整数回退为默认值 3。
- 该值是部署级配置，不接受 job options 覆盖，避免普通请求绕过服务端资源限制。
- 运维可将其设为 `1` 恢复逐页串行行为，无需回滚版本。

现有 `MAX_CONCURRENT_JOBS=4` 保持不变。因此默认单 worker 的理论 PDF OCR Provider 请求峰值为 12；模型端配额或服务器内存较小时，应下调其中一个配置。PDFium 渲染峰值不随该配置相乘：每个进程任意时刻只允许一个 PDFium 渲染调用。

## 组件与数据流

### 页面检查

现有页面检查逻辑不变：

- 原生文字充足的页面直接保留文字，不进入工作池；
- 无文本层或整页图片上只有少量文字的页面进入 OCR 队列。

### 页级工作池

PDF handler 为每份 PDF 建立配置数量的异步 worker（默认 3，上限 8），实际 worker 数量为“配置值”和“OCR 页面数”的较小值。每个 worker 执行：

1. 领取下一个待 OCR 的页面状态；
2. 在线程池中等待进程级 PDFium mutex，持有 mutex 时渲染该页，避免阻塞事件循环并禁止 PDFium 并发；
3. 调用注入的 `OCRProvider`；
4. 将识别文字写入以原始页码索引的结果槽；
5. 在 `finally` 中删除本页临时 PNG；
6. 继续领取下一页。

不同页面使用 `ocr-page-{页码}.png`，不会互相覆盖。各 job 仍使用独立 workdir。

PDFium mutex 的范围到 PNG 完整写入为止，不包含 `OCRProvider.extract_text(...)`。因此前一页进入 Provider 等待后，后续页可以依次渲染并进入同一 Provider 并发窗口。

### 顺序与输出

原生文本和 OCR 文本都写入按总页数预分配的结果列表。所有 worker 完成后按页码顺序拼接，因此并发完成顺序不会影响 TXT 或 Word 的页序。

PDF → TXT 和含扫描页的 PDF → DOCX 共用同一文本提取流程，所以两类输出同时获得并发加速。纯文字 PDF → DOCX 仍走 `pdf2docx`，不启动 OCR 工作池。

## 失败和取消语义

- 页面渲染失败继续返回 `CONVERSION_ENGINE_ERROR`。
- OCR Provider 失败继续保留其结构化错误，例如 `OCR_FAILED`。
- 任一页面失败后设置停止标记，不再领取尚未开始的新页面。
- 已经开始的页面（包括正在等待 PDFium mutex 的渲染线程）允许完成，以保证线程渲染结束后再安全删除临时文件；如果同时记录多个错误，按原始页码选择最前一页的错误抛出，使结果可重复。
- job 的现有 20 分钟 PDF 超时保持不变。渲染调用使用可等待的独立 task；如果 job 在 `asyncio.to_thread` 渲染期间被取消，协程先等待该渲染线程结束，再删除临时图片并继续传播取消。Provider 调用期间被取消则直接进入 `finally` 清理。job runner 在 handler 完成取消后再清理 workdir，作为最终兜底。

这里不在首次失败时直接取消正在执行的 `asyncio.to_thread`：取消 await 不会停止底层渲染线程，过早删除文件可能造成线程稍后重新写出残留图片。

## 资源影响

250 DPI 页面在渲染和编码时会占用明显内存，但 PDFium 渲染在每个进程内全局串行。默认并发 3 指 OCR Provider 并发，上限 8 用于防止单个任务同时持有过多已渲染图片和 Provider 请求。

并发不会改变模型调用次数或单页 token 上限，只会让调用时间重叠。模型服务存在每秒请求数限制时，运维应降低 `PDF_OCR_PAGE_CONCURRENCY`；Provider 返回限流错误时仍按现有 OCR 失败路径处理，本次不新增自动重试。

## 可观测性与文档

- PDF OCR 开始时记录扫描页数和当前 OCR worker/Provider 并发数，不记录页面内容、图片数据或凭证。该值不表示 PDFium 渲染并发数。
- `.env.example`、README 和部署文档增加配置说明，以及任务级并发与页级并发相乘的容量提示。
- 不改变前端交互、Registry、上传接口、任务状态接口和下载接口。

## 测试与验收

### 自动化测试

1. 使用带 barrier 的 fake OCR Provider 验证真实多页 PDF 在 PDFium 渲染串行时，Provider 峰值并发仍为 3。
2. 配置为 2 时，峰值不得超过 2；配置为 1 时保持串行。
3. 让不同页面以不同顺序完成，验证 TXT 和 DOCX 仍按原始页码排序。
4. 混合 PDF 中原生文字页不调用 OCR，扫描页并发处理，合并顺序正确。
5. 某页 OCR 失败后不再领取新页面，已开始页面完成清理，最终错误码仍为 `OCR_FAILED`。
6. 转换完成或失败后，workdir 中不存在 `ocr-page-*.png`。
7. 运行完整后端测试套件，确认现有转换路线不回归。
8. 在隔离子进程中用三个线程真实调用生产 `_render_pdf_page`，断言子进程 exit 0 且三个 PNG 均存在、非空，防止删除进程级 PDFium mutex。

### Docker 验证

1. 使用无缓存或明确包含新代码的方式重新构建 API/worker 镜像。
2. 启动新容器并确认 worker 健康运行。
3. 使用真实多页扫描 PDF 分别生成 TXT 和 DOCX，检查文件可打开、文字可编辑、页序正确。
4. 从日志或受控测试 Provider 的时间记录确认至少 2 个页面调用发生重叠。

## 不在本次范围

- 新增跨任务、跨进程或跨服务器的全局 OCR/Provider 限流；PDFium 进程内全局互斥是安全必需，不属于此禁止项。
- OCR Provider 自动重试、指数退避或配额感知调度。
- 将多个 PDF 页面合并为一次多图模型请求。
- 扫描 PDF → XLSX/PPTX 的结构化 OCR。
- 前端让单个用户选择并发数。
