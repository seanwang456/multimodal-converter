# SQLite 首次启动并发初始化修复设计

## 目标

消除空 SQLite 数据库上 API 与 worker 同时启动时的建表竞争，保证两个进程均可一次启动成功；不修改现有业务表结构、不引入数据库迁移，也不影响正常任务读写性能。

## 现状与根因

API 与 worker 启动时都会调用 `app.database.init_db()`。该函数直接执行 `SQLModel.metadata.create_all(engine)`。`create_all()` 会先检查表是否存在，再执行建表；两个独立进程可能同时判断表不存在，随后其中一个成功建表，另一个收到 `sqlite3.OperationalError: table ... already exists` 并退出。

现有 `PRAGMA busy_timeout=5000` 只能等待 SQLite 写锁，不能让“检查表是否存在 + 建表”成为跨进程原子操作。使用四个进程同时初始化空数据库的诊断中，10 轮有 8 轮出现至少一个进程失败。

该问题主要影响全新部署、空存储卷和数据库文件被移除后的首次启动。已有数据库的日常启动通常不会触发；发生时已成功建表的进程会留下可用数据库，失败进程重启后一般可恢复，因此当前证据不指向数据损坏。

## 方案选择

采用“共享卷文件锁 + Compose 健康依赖”的组合：

1. `init_db()` 在 SQLite 文件同目录打开固定锁文件 `<数据库文件名>.init.lock`。
2. 使用 `fcntl.flock(..., LOCK_EX)` 获取跨进程排他锁。
3. 仅在持锁期间执行模型注册和 `SQLModel.metadata.create_all(engine)`，完成或异常后由上下文管理自动释放锁。
4. Docker Compose 中 worker 对 API 的依赖由 `service_started` 改为 `service_healthy`，避免 worker 在 API 尚未完成初始化时提前进入业务启动流程。

文件锁是根因修复：无论通过 Compose、手工命令还是其他进程管理器启动，所有调用 `init_db()` 的进程都会串行初始化。Compose 健康依赖是额外防护，并不替代文件锁。

没有选择以下方案：

- 只调整 Compose 顺序：无法覆盖非 Compose 启动和未来新增调用者。
- 捕获并忽略 `table already exists`：可能掩盖部分建表成功、其他表失败等真实数据库错误。
- 新增第三方锁库：当前 Docker/Linux 和开发用 macOS 均原生支持 `fcntl`，没有必要增加依赖。

## 锁的边界与异常行为

- 锁文件位于 SQLite 数据目录，API 与 worker 已共享同一个 Docker volume，因此能竞争同一把锁。
- 锁只覆盖初始化，不包围普通 Session、任务查询或转换结果写入。
- `create_all()` 抛出的真实异常继续原样向上抛出；锁必须在异常路径释放。
- 锁文件可以长期存在；进程退出或文件描述符关闭时内核自动释放锁，不依赖删除锁文件。
- 本次部署目标为 Linux Docker，开发环境为 macOS，二者均支持 `fcntl`。不扩展 Windows 原生运行支持。

## 测试与验收

1. 新增真实多进程回归测试：四个独立 Python 进程同时对同一个空 SQLite 文件调用 `init_db()`，要求全部退出码为 0。
2. 回归测试还要打开最终数据库，确认核心业务表存在，避免仅验证“没有抛异常”。
3. 测试必须先在未修复代码上稳定失败，再实施生产代码修改。
4. 运行完整后端测试套件和静态编译检查。
5. 检查 `docker compose config`，确认 worker 等待 API 健康。
6. 使用全新命名卷同时启动 Redis、API 和 worker，要求 API 与 worker 均第一次保持运行、健康检查通过，日志无重复建表异常。

## 发布与兼容性

- 不修改数据库 schema，不需要迁移或恢复数据。
- 不覆盖 `.env`，不改变存储卷和现有任务数据。
- 回滚到旧提交不会受残留锁文件影响；旧版本不会读取该文件。
- 本修复与扫描 PDF OCR 代码位于同一功能分支，验证后更新现有 PR。

## 不在本次范围

- 引入 Alembic 或其他正式迁移框架。
- 改用 PostgreSQL 等外部数据库。
- 修复与首次数据库初始化无关的 Web 代理或文档字体问题。
