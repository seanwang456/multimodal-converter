"""阿里云百炼「千问3-ASR-Flash-Filetrans」ASRProvider（DashScope 异步调用）。

文档：help.aliyun.com/zh/model-studio/qwen-asr（非实时语音识别 / 录音文件识别）。
流程为「提交音频 URL + 轮询 task + 下载 transcription_url」，与火山同属"公网回拉"模式，
故复用 `app.providers.asr_source` 的签名下载 token 与 `/api/asr-source/{token}` 端点。

接入要点：
- 提交：POST {host}/api/v1/services/audio/asr/transcription，必须带 `X-DashScope-Async: enable`，
  body = {model, input:{file_url}, parameters:{channel_id:[0], enable_itn, language?}}，返回 output.task_id。
- 轮询：GET {host}/api/v1/tasks/{task_id}，output.task_status ∈ PENDING/RUNNING/SUCCEEDED/FAILED/UNKNOWN。
- 成功后 output.result.transcription_url 是一个 JSON 文件（24h 有效），内含 transcripts[].text 与 sentences[]。

host 默认从 LLM_BASE_URL 推导（去掉 /compatible-mode/v1 后缀）；key 默认复用 LLM_API_KEY。
"""
from __future__ import annotations

import asyncio
import time

from app.config import settings
from app.errors import ConversionError, ErrorCode
from app.providers.asr_source import rel_under_storage, require_public_base, sign_source_token
from app.providers.base import ASRProvider, ASRResult

_SUBMIT_PATH = "/api/v1/services/audio/asr/transcription"
_POLL_PATH = "/api/v1/tasks/"  # + task_id


def _host() -> str:
    """阿里云 MaaS 主机：优先 ALIYUN_ASR_BASE_URL，否则从 LLM_BASE_URL 去掉兼容模式后缀。"""
    host = (settings.aliyun_asr_base_url or settings.llm_base_url or "").rstrip("/")
    for suffix in ("/compatible-mode/v1", "/compatible-mode", "/v1"):
        if host.endswith(suffix):
            host = host[: -len(suffix)]
            break
    if not host:
        raise ConversionError(ErrorCode.ASR_FAILED, "未配置阿里云 ASR 主机（LLM_BASE_URL 或 ALIYUN_ASR_BASE_URL）")
    return host


def _key() -> str:
    key = settings.aliyun_asr_api_key or settings.llm_api_key
    if not key:
        raise ConversionError(ErrorCode.ASR_FAILED, "未配置阿里云 API Key（LLM_API_KEY 或 ALIYUN_ASR_API_KEY）")
    return key


class AliyunFiletransASRProvider(ASRProvider):
    async def transcribe(
        self, audio_path: str, language: str = "auto",
        timestamps: bool = True, speaker_labels: bool = False,
    ) -> ASRResult:
        host = _host()
        _key()  # 触发缺 key 校验
        base = require_public_base(public_base_url=settings.public_base_url)

        file_url = f"{base}/api/asr-source/{sign_source_token(rel_under_storage(audio_path, storage_root=settings.storage_root))}"

        import httpx
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                task_id = await self._submit(client, host, file_url, language)
                text, segs, duration = await self._poll(client, host, task_id)
        except ConversionError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ConversionError(ErrorCode.ASR_FAILED, "阿里云 ASR 调用失败（网络）") from e

        return ASRResult(
            text=text, segments=segs,
            language=language if language != "auto" else "zh",
            duration_seconds=duration,
        )

    # ---- 内部 ----

    async def _submit(self, client, host: str, file_url: str, language: str) -> str:
        parameters: dict = {"channel_id": [0], "enable_itn": True}
        if language and language != "auto":
            parameters["language"] = language
        body = {
            "model": settings.aliyun_asr_model,
            "input": {"file_url": file_url},
            "parameters": parameters,
        }
        headers = {
            "Authorization": f"Bearer {_key()}",
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",  # 异步调用必带，否则报 "does not support synchronous calls"
        }
        r = await client.post(host + _SUBMIT_PATH, json=body, headers=headers)
        try:
            data = r.json() or {}
        except Exception:  # noqa: BLE001
            data = {}
        output = data.get("output") or {}
        task_id = output.get("task_id")
        if not task_id:
            code = data.get("code") or r.status_code
            msg = data.get("message") or r.text[:200]
            raise ConversionError(ErrorCode.ASR_FAILED, f"阿里云 ASR 提交失败：{code} {msg}".strip())
        return task_id

    async def _poll(self, client, host: str, task_id: str) -> tuple[str, list, float]:
        headers = {"Authorization": f"Bearer {_key()}", "Content-Type": "application/json"}
        deadline = time.time() + settings.asr_query_timeout_seconds
        interval = settings.asr_query_interval_seconds
        last = "PENDING"
        while time.time() < deadline:
            r = await client.get(host + _POLL_PATH + task_id, headers=headers)
            try:
                data = r.json() or {}
            except Exception:  # noqa: BLE001
                data = {}
            output = data.get("output") or {}
            status = output.get("task_status") or "UNKNOWN"
            last = status
            if status == "SUCCEEDED":
                transcription_url = (output.get("result") or {}).get("transcription_url")
                duration = float((data.get("usage") or {}).get("seconds") or 0)
                if not transcription_url:
                    raise ConversionError(ErrorCode.ASR_FAILED, "阿里云 ASR 成功但未返回 transcription_url")
                return await self._fetch_result(client, transcription_url, duration)
            if status == "FAILED":
                code = output.get("code") or "FAILED"
                msg = output.get("message") or ""
                raise ConversionError(ErrorCode.ASR_FAILED, f"阿里云 ASR 失败：{code} {msg}".strip())
            if status == "UNKNOWN":
                raise ConversionError(ErrorCode.ASR_FAILED, "阿里云 ASR 任务不存在或状态未知")
            # PENDING / RUNNING → 继续轮询
            await asyncio.sleep(interval)
        raise ConversionError(ErrorCode.CONVERSION_TIMEOUT, f"阿里云 ASR 轮询超时（最后状态 {last}）")

    async def _fetch_result(self, client, transcription_url: str, duration: float) -> tuple[str, list, float]:
        r = await client.get(transcription_url)
        try:
            data = r.json() or {}
        except Exception as e:  # noqa: BLE001
            raise ConversionError(ErrorCode.ASR_FAILED, "阿里云 ASR 结果文件解析失败") from e
        transcripts = data.get("transcripts") or []
        text = " ".join((t.get("text") or "").strip() for t in transcripts).strip()
        segs: list = []
        for t in transcripts:
            for s in (t.get("sentences") or []):
                segs.append({
                    "start": (s.get("begin_time") or 0) / 1000.0,
                    "end": (s.get("end_time") or 0) / 1000.0,
                    "text": (s.get("text") or "").strip(),
                })
        if duration <= 0 and segs:
            duration = segs[-1]["end"]
        return text, segs, duration
