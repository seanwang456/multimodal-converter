"""异步子进程执行：超时即 kill 并等待清理，避免僵尸进程。"""
from __future__ import annotations

import asyncio


async def run_subprocess(cmd: list[str], timeout: float) -> tuple[int, bytes, bytes]:
    """运行子进程；超时 kill 子进程并等待退出后抛 asyncio.TimeoutError。返回 (returncode, stdout, stderr)。"""
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode, out, err
