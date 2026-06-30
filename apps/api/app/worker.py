"""转换 worker：消费 Redis 队列、有界并发（每 job 独立 task）、启动重扫在途、周期清理。

入口：python -m app.worker
"""
from __future__ import annotations

import asyncio
import logging

import redis as redis_lib
from sqlmodel import Session, select

from app.config import settings
from app.database import engine, init_db
from app.models import ConversionJob
from app.services import registry, storage
from app.services.job_runner import QUEUE_NAME, process_job

log = logging.getLogger("converter.worker")


def reenqueue_inflight(redis_client: redis_lib.Redis) -> int:
    """启动时重扫 queued/running 任务重新入队（捕获跨重启持久）。"""
    with Session(engine) as s:
        jobs = s.exec(
            select(ConversionJob).where(ConversionJob.status.in_(["queued", "running"]))
        ).all()
        for j in jobs:
            redis_client.rpush(QUEUE_NAME, j.id)
        return len(jobs)


async def _run_one(sem: asyncio.Semaphore, job_id: str) -> None:
    """单个任务执行：异常不外泄；finally 释放在 _worker_loop 预占的信号量名额。"""
    try:
        await process_job(job_id)
    except Exception:  # noqa: BLE001
        log.exception("process_job 失败 job=%s", job_id)
    finally:
        sem.release()


async def _worker_loop(sem: asyncio.Semaphore) -> None:
    """消费循环：先 acquire 占名额再 dequeue（dequeue 背压，pending task 有界=并发上限）。

    注意：redis-py 部分版本在 BLPOP 超时（队列空）时会抛 TimeoutError 而非返回 None，
    必须捕获后按“无任务”处理，否则消费循环会崩溃导致任务永久 queued。
    """
    redis_client = redis_lib.Redis.from_url(settings.redis_url, socket_connect_timeout=5)
    loop = asyncio.get_running_loop()
    while True:
        await sem.acquire()
        try:
            item = await loop.run_in_executor(
                None, lambda: redis_client.blpop(QUEUE_NAME, timeout=5)
            )
        except redis_lib.exceptions.TimeoutError:
            # BLPOP 等待超时（队列空）的另一种返回形式，按无任务处理
            sem.release()
            continue
        except redis_lib.exceptions.ConnectionError as e:
            log.warning("redis 连接异常，5s 后重连：%s", e)
            sem.release()
            await asyncio.sleep(5)
            try:
                redis_client = redis_lib.Redis.from_url(settings.redis_url, socket_connect_timeout=5)
            except Exception:  # noqa: BLE001
                pass
            continue
        if item is None:
            sem.release()
            continue
        _, raw = item
        job_id = raw.decode() if isinstance(raw, bytes) else raw
        asyncio.create_task(_run_one(sem, job_id))


async def _cleanup_loop() -> None:
    """周期清理：失败不外泄，绝不能拖垮任务消费。"""
    while True:
        await asyncio.sleep(settings.cleanup_interval_seconds)
        try:
            with Session(engine) as s:
                removed = storage.sweep_expired(s)
                if removed:
                    log.info("清理过期文件 %d 条", removed)
            tmp = storage.sweep_asr_tmp()
            if tmp:
                log.info("清理 ASR 临时音频 %d 个", tmp)
        except Exception:  # noqa: BLE001
            log.exception("周期清理失败（已忽略，继续消费任务）")


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings.ensure_dirs()
    init_db()
    registry.register_handlers()
    registry.self_check()  # worker 启动也跑自检（fail-fast）

    redis_client = redis_lib.Redis.from_url(settings.redis_url)
    redis_client.ping()
    n = reenqueue_inflight(redis_client)
    log.info("worker 启动，重扫在途任务 %d 条，并发上限 %d", n, settings.max_concurrent_jobs)

    sem = asyncio.Semaphore(settings.max_concurrent_jobs)
    await asyncio.gather(_worker_loop(sem), _cleanup_loop())


if __name__ == "__main__":
    asyncio.run(main())
