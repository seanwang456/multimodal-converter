"""火山豆包「录音文件识别（标准版）」ASRProvider。规格 §15。

与 Doubao chat 端点不同：本服务为异步「提交音频 URL + 轮询查询」模式，火山服务器
从公网回拉音频文件（不接受二进制上传）。文档：
https://www.volcengine.com/docs/6561/1354868

公网回拉所需的签名 token / 路径辅助已抽到 `app.providers.asr_source`（与阿里云 filetrans 共用）。
鉴权（新版控制台，单 key）：X-Api-Key + X-Api-Resource-Id + X-Api-Request-Id（+ 提交时 X-Api-Sequence=-1）。
状态码在 response header `X-Api-Status-Code`：20000000 成功 / 01 处理中 / 02 排队 / 03 静音 / 45000001 参数无效 / 550xxxx 内部错误。
"""
from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

from app.config import settings
from app.errors import ConversionError, ErrorCode
from app.providers.base import ASRProvider, ASRResult

# 共享：签名 token、路径、公网校验（向后兼容重导出，供旧测试/路由引用）
from app.providers.asr_source import (  # noqa: F401
    is_local_url,
    rel_under_storage,
    require_public_base,
    sign_source_token,
    verify_source_token,
)

_SUBMIT_PATH = "/submit"
_QUERY_PATH = "/query"

# 火山接受的容器格式：扩展名 -> 提交 format 字段
_ACCEPTED = {".mp3": "mp3", ".wav": "wav", ".ogg": "ogg"}
# 转码 mp3 比特率（语音 64k 足够，减小回拉体积）
_TRANSCODE_BIN_KB = 64


async def _ensure_accepted(audio_path: str) -> tuple[Path, str, bool]:
    """归一化到火山接受的格式。返回 (待提交路径, format, is_staged)。

    mp3/wav/ogg 直接用原文件；其余（m4a/aac/...）ffmpeg 转 16k mono mp3 到 storage/asr_tmp。
    """
    src = Path(audio_path)
    ext = src.suffix.lower()
    if ext in _ACCEPTED:
        return src, _ACCEPTED[ext], False

    staged = settings.storage_root.resolve() / "asr_tmp" / f"{uuid.uuid4().hex}.mp3"
    staged.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        settings.ffmpeg_bin, "-y", "-i", str(src), "-vn",
        "-ar", "16000", "-ac", "1", "-b:a", f"{_TRANSCODE_BIN_KB}k",
        "-f", "mp3", str(staged),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        raise ConversionError(ErrorCode.CONVERSION_ENGINE_ERROR, "ffmpeg 未安装，无法转码音频") from e
    try:
        await asyncio.wait_for(proc.wait(), timeout=180)
    except asyncio.TimeoutError:
        proc.kill()
        raise ConversionError(ErrorCode.CONVERSION_TIMEOUT, "音频转码超时") from None
    if proc.returncode != 0 or not staged.exists():
        err = (await proc.stderr.read()).decode(errors="replace")[:200] if proc.stderr else ""
        raise ConversionError(ErrorCode.CONVERSION_ENGINE_ERROR, f"音频转码失败：{err}")
    return staged, "mp3", True


class VolcanoBigModelASRProvider(ASRProvider):
    async def transcribe(
        self, audio_path: str, language: str = "auto",
        timestamps: bool = True, speaker_labels: bool = False,
    ) -> ASRResult:
        api_key = settings.volcano_asr_api_key
        if not api_key:
            raise ConversionError(ErrorCode.ASR_FAILED, "未配置火山 ASR（VOLCANO_ASR_API_KEY）")
        base = require_public_base(public_base_url=settings.public_base_url)

        staged: Path | None = None
        try:
            audio, fmt, is_staged = await _ensure_accepted(audio_path)
            staged = audio if is_staged else None
            audio_url = f"{base}/api/asr-source/{sign_source_token(rel_under_storage(str(audio), storage_root=settings.storage_root))}"

            import httpx
            request_id = str(uuid.uuid4())
            try:
                async with httpx.AsyncClient(timeout=60) as client:
                    await self._submit(client, request_id, audio_url, fmt, language)
                    text, segs, duration = await self._poll(client, request_id)
            except ConversionError:
                raise
            except Exception as e:  # noqa: BLE001
                raise ConversionError(ErrorCode.ASR_FAILED, "火山 ASR 调用失败（网络）") from e
        finally:
            if staged is not None:
                staged.unlink(missing_ok=True)

        return ASRResult(
            text=text, segments=segs,
            language=language if language != "auto" else "zh",
            duration_seconds=duration,
        )

    # ---- 内部 ----

    def _headers(self, request_id: str, submit: bool) -> dict[str, str]:
        h = {
            "X-Api-Key": settings.volcano_asr_api_key,
            "X-Api-Resource-Id": settings.volcano_asr_resource_id,
            "X-Api-Request-Id": request_id,
        }
        if submit:
            h["X-Api-Sequence"] = "-1"
        return h

    async def _submit(self, client, request_id: str, audio_url: str, fmt: str, language: str) -> None:
        audio_spec: dict = {"format": fmt, "url": audio_url}
        if language and language != "auto":
            audio_spec["language"] = language
        body = {
            "user": {"uid": "converter"},
            "audio": audio_spec,
            "request": {"model_name": "bigmodel", "enable_itn": True, "enable_punc": True},
        }
        r = await client.post(
            settings.volcano_asr_endpoint + _SUBMIT_PATH, json=body, headers=self._headers(request_id, submit=True),
        )
        self._ensure_status(r, "提交")

    async def _poll(self, client, request_id: str) -> tuple[str, list, float]:
        endpoint = settings.volcano_asr_endpoint + _QUERY_PATH
        headers = self._headers(request_id, submit=False)
        deadline = time.time() + settings.asr_query_timeout_seconds
        interval = settings.asr_query_interval_seconds
        last = "20000001"
        while time.time() < deadline:
            r = await client.post(endpoint, json={}, headers=headers)
            code = r.headers.get("X-Api-Status-Code", "")
            if code == "20000000":
                return self._parse(r)
            if code in ("20000001", "20000002"):  # 处理中 / 排队中
                last = code
                await asyncio.sleep(interval)
                continue
            if code == "20000003":  # 静音音频：无语音，返回空
                return "", [], 0.0
            raise ConversionError(
                ErrorCode.ASR_FAILED,
                f"火山 ASR 查询失败：{code} {r.headers.get('X-Api-Message', '')}".strip(),
            )
        raise ConversionError(ErrorCode.CONVERSION_TIMEOUT, f"火山 ASR 轮询超时（最后状态 {last}）")

    def _ensure_status(self, r, stage: str) -> None:
        code = r.headers.get("X-Api-Status-Code", "")
        if code != "20000000":
            raise ConversionError(
                ErrorCode.ASR_FAILED,
                f"火山 ASR{stage}失败：{code} {r.headers.get('X-Api-Message', '')}".strip(),
            )

    def _parse(self, r) -> tuple[str, list, float]:
        try:
            body = r.json() or {}
        except Exception:  # noqa: BLE001
            body = {}
        result = body.get("result") or {}
        text = (result.get("text") or "").strip()
        segs = [
            {
                "start": (u.get("start_time") or 0) / 1000.0,
                "end": (u.get("end_time") or 0) / 1000.0,
                "text": (u.get("text") or "").strip(),
            }
            for u in (result.get("utterances") or [])
        ]
        duration = ((body.get("audio_info") or {}).get("duration") or 0) / 1000.0
        return text, segs, duration
