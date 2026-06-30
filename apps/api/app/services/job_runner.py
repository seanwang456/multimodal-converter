"""JobRunner 抽象 + Redis/Inline 实现 + process_job 状态机。

process_job 可独立同步调用（测试/无 Redis 环境）以验证完整状态迁移。
Worker 消费循环见 app/worker.py。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path
from sqlmodel import Session

from app.config import settings
from app.database import engine
from app.errors import ConversionError, ErrorCode
from app.filetypes import category_of
from app.models import ConversionJob, FileRecord, _now
from app.services import registry, storage

log = logging.getLogger("converter.runner")

QUEUE_NAME = "converter:jobs"

# 按源类别超时（秒）—— 规格 §18
TIMEOUT_BY_CATEGORY = {"image": 180, "audio": 600, "video": 1200, "pdf": 1200}
DEFAULT_TIMEOUT = 180


def timeout_for(source_ext: str, target_ext: str) -> int:  # noqa: ARG001
    return TIMEOUT_BY_CATEGORY.get(category_of(source_ext), DEFAULT_TIMEOUT)


class JobRunner:
    """任务提交/状态抽象。"""

    def submit(self, job_id: str) -> None:
        raise NotImplementedError

    def get_state(self, job_id: str) -> dict | None:
        with Session(engine) as s:
            j = s.get(ConversionJob, job_id)
            if not j:
                return None
            return {"status": j.status, "progress": j.progress}


class InlineJobRunner(JobRunner):
    """同步执行：用于测试与无 Redis 的本地验证。"""

    def submit(self, job_id: str) -> None:
        asyncio.run(process_job(job_id))


class RedisJobRunner(JobRunner):
    """生产实现：入 Redis 队列，由 worker 消费。"""

    def __init__(self, redis_client) -> None:  # noqa: ANN001
        self.redis = redis_client

    def submit(self, job_id: str) -> None:
        self.redis.rpush(QUEUE_NAME, job_id)


_runner: JobRunner | None = None


def build_runner() -> JobRunner:
    """按 JOB_RUNNER_MODE 构造 runner。

    - inline：强制同步执行（测试/本地）
    - redis：强制 Redis，不可用即抛错
    - auto（默认）：优先 Redis；不可用时开发环境回退 Inline，否则 fail-fast
    """
    mode = os.getenv("JOB_RUNNER_MODE", "auto").lower()

    def _try_redis() -> JobRunner:
        import redis as redis_lib

        client = redis_lib.from_url(settings.redis_url)
        client.ping()
        return RedisJobRunner(client)

    if mode == "inline":
        return InlineJobRunner()
    if mode == "redis":
        return _try_redis()  # 不可用直接抛
    # auto
    try:
        return _try_redis()
    except Exception:  # noqa: BLE001
        if settings.app_env == "development":
            log.warning("Redis 不可用，开发环境回退 Inline（同步执行，生产环境应配 Redis）")
            return InlineJobRunner()
        raise ConversionError(ErrorCode.CONVERSION_ENGINE_ERROR, "任务队列不可用")


def get_runner() -> JobRunner:
    global _runner
    if _runner is None:
        _runner = build_runner()
    return _runner


def _mark_failed(job_id: str, code: str, message: str) -> None:
    with Session(engine) as s:
        j = s.get(ConversionJob, job_id)
        if not j:
            return
        j.status = "failed"
        j.progress = 100
        j.message = "转换失败"
        j.error_code = code
        j.error_message = message
        j.finished_at = _now()
        j.updated_at = _now()
        s.add(j)
        s.commit()


async def process_job(job_id: str) -> None:
    """核心转换执行：running→succeeded/failed，按类型超时，透传 options。"""
    with Session(engine) as s:
        job = s.get(ConversionJob, job_id)
        if not job:
            return
        # 幂等守卫：重复投递或重启重扫时，已终态任务不再处理
        if job.status in {"succeeded", "failed", "expired"}:
            return
        file = s.get(FileRecord, job.file_id)
        if not file:
            _mark_failed(job_id, ErrorCode.NOT_FOUND, "源文件不存在")
            return
        job.status = "running"
        job.progress = 10
        job.message = "转换中"
        job.updated_at = _now()
        s.add(job)
        s.commit()
        handler_key = job.handler_key
        src_ext = job.source_ext
        tgt_ext = job.target_ext
        file_stored = file.stored_path
        orig_name = file.original_filename
        try:
            options = json.loads(job.options) if job.options else {}
        except Exception:  # noqa: BLE001
            options = {}

    workdir: Path | None = None
    try:
        handler = registry.get_handler(handler_key)
        if handler is None:
            raise ConversionError(ErrorCode.CONVERSION_ENGINE_ERROR, "handler 未注册")
        workdir = storage.create_workdir(job_id)
        result = await asyncio.wait_for(
            handler.run(
                str(storage.upload_abspath(file_stored)),
                str(workdir),
                tgt_ext,
                options,
            ),
            timeout_for(src_ext, tgt_ext),
        )
        out = Path(result["output_path"])
        if not out.exists() or out.stat().st_size <= 0:
            raise ConversionError(ErrorCode.CONVERSION_ENGINE_ERROR, "handler 未产出有效文件")
        dest = storage.result_abspath(job_id, tgt_ext)
        shutil.move(str(out), str(dest))
        size = dest.stat().st_size
        stem = storage.sanitize_filename(Path(orig_name).stem) or "result"
        result_name = f"{stem}{tgt_ext}"
        with Session(engine) as s:
            j = s.get(ConversionJob, job_id)
            j.status = "succeeded"
            j.progress = 100
            j.message = "转换完成"
            j.result_path = f"results/{job_id}{tgt_ext}"
            j.result_filename = result_name
            j.result_size_bytes = size
            j.quality_notice = result.get("quality_notice")
            j.finished_at = _now()
            j.updated_at = _now()
            s.add(j)
            s.commit()
    except asyncio.TimeoutError:
        _mark_failed(job_id, ErrorCode.CONVERSION_TIMEOUT, "转换超时，请尝试较小文件")
    except ConversionError as e:
        _mark_failed(job_id, e.code, e.message)
    except Exception:  # noqa: BLE001
        log.exception("process_job 未捕获异常 job=%s", job_id)
        _mark_failed(job_id, ErrorCode.CONVERSION_ENGINE_ERROR, "转换服务异常，请稍后重试")
    finally:
        if workdir:
            storage.cleanup_workdir(job_id)
