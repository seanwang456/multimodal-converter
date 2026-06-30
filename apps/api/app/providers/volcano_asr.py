"""火山豆包「录音文件识别（标准版）」ASRProvider。规格 §15。

与 Doubao chat 端点不同：本服务为异步「提交音频 URL + 轮询查询」模式，火山服务器
从公网回拉音频文件（不接受二进制上传）。文档：
https://www.volcengine.com/docs/6561/1354868

因此需要两件事配合：
1. PUBLIC_BASE_URL —— 应用对外可达的公网基址（Caddy 域名 / 隧道），用于拼装音频 URL；
2. GET /api/asr-source/{token} —— 无状态 HMAC 签名的临时音频下载端点（见 routers/asr_source.py），
   token 由本模块 `sign_source_token` 生成、`verify_source_token` 校验，worker 与 api 共享同一密钥。

鉴权（新版控制台，单 key）：X-Api-Key + X-Api-Resource-Id + X-Api-Request-Id（+ 提交时 X-Api-Sequence=-1）。
状态码在 response header `X-Api-Status-Code`：20000000 成功 / 01 处理中 / 02 排队 / 03 静音 / 45000001 参数无效 / 550xxxx 内部错误。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from app.config import settings
from app.errors import ConversionError, ErrorCode
from app.providers.base import ASRProvider, ASRResult

_SUBMIT_PATH = "/submit"
_QUERY_PATH = "/query"

# 火山接受的容器格式：扩展名 -> (提交 format 字段, 下载 content-type)
_ACCEPTED = {
    ".mp3": ("mp3", "audio/mpeg"),
    ".wav": ("wav", "audio/wav"),
    ".ogg": ("ogg", "audio/ogg"),
}
# 不在 _ACCEPTED 内的源格式（m4a/aac/flac/...）需 ffmpeg 转码到 mp3
_TRANSCODE_BIN_KB = 64  # 转码 mp3 比特率（语音 64k 足够，减小回拉体积）


# ---------------- 无状态签名 token（worker 签发，api 校验）----------------

def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _secret() -> str:
    return settings.asr_source_secret or settings.volcano_asr_api_key or "dev-insecure-secret"


def sign_source_token(rel_path: str, ttl: int | None = None) -> str:
    """生成 HMAC 签名的音频下载 token：`exp.payload.sig`。

    payload = base64url(相对 storage_root 的路径)；sig = HMAC-SHA256(secret, "exp.payload")。
    无状态：api 端只需同一 secret 即可校验，不依赖 Redis/DB。
    """
    ttl = settings.asr_source_ttl_seconds if ttl is None else ttl
    exp = int(time.time()) + ttl
    payload = _b64url(rel_path.encode())
    sig = _b64url(hmac.new(_secret().encode(), f"{exp}.{payload}".encode(), hashlib.sha256).digest())
    return f"{exp}.{payload}.{sig}"


def verify_source_token(token: str) -> tuple[str, bool]:
    """校验签名与过期，返回 (rel_path, ok)。签名不符或已过期均返回 ("", False)。"""
    parts = token.split(".")
    if len(parts) != 3:
        return "", False
    exp_str, payload, sig = parts
    expected = _b64url(hmac.new(_secret().encode(), f"{exp_str}.{payload}".encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, sig):
        return "", False
    try:
        if int(exp_str) < time.time():
            return "", False
        rel = _b64url_decode(payload).decode()
    except Exception:  # noqa: BLE001
        return "", False
    return rel, True


# ---------------- 辅助 ----------------

def _is_local(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "0.0.0.0", "::1", "")


def _rel_under_storage(audio_path: str) -> str:
    """计算相对 storage_root 的 posix 路径；不在其下则拒绝（无法生成公网链接）。"""
    p = Path(audio_path).resolve()
    root = settings.storage_root.resolve()
    try:
        return p.relative_to(root).as_posix()
    except ValueError as exc:
        raise ConversionError(
            ErrorCode.ASR_FAILED, "音频不在存储目录内，无法生成公网下载链接"
        ) from exc


async def _ensure_accepted(audio_path: str) -> tuple[Path, str, bool]:
    """归一化到火山接受的格式。返回 (待提交路径, format, is_staged)。

    mp3/wav/ogg 直接用原文件；其余（m4a/aac/...）ffmpeg 转 16k mono mp3 到 storage/asr_tmp。
    """
    src = Path(audio_path)
    ext = src.suffix.lower()
    if ext in _ACCEPTED:
        return src, _ACCEPTED[ext][0], False

    root = settings.storage_root.resolve()
    staged = root / "asr_tmp" / f"{uuid.uuid4().hex}.mp3"
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
        err = ""
        if proc.stderr:
            err = (await proc.stderr.read()).decode(errors="replace")[:200]
        raise ConversionError(ErrorCode.CONVERSION_ENGINE_ERROR, f"音频转码失败：{err}")
    return staged, "mp3", True


# ---------------- provider ----------------

class VolcanoBigModelASRProvider(ASRProvider):
    async def transcribe(
        self, audio_path: str, language: str = "auto",
        timestamps: bool = True, speaker_labels: bool = False,
    ) -> ASRResult:
        api_key = settings.volcano_asr_api_key
        if not api_key:
            raise ConversionError(ErrorCode.ASR_FAILED, "未配置火山 ASR（VOLCANO_ASR_API_KEY）")
        base = (settings.public_base_url or "").rstrip("/")
        if not base or _is_local(base):
            raise ConversionError(
                ErrorCode.ASR_FAILED,
                "ASR 需要公网可达的 PUBLIC_BASE_URL（当前为本地/空，火山无法回拉音频）",
            )

        staged: Path | None = None
        try:
            audio, fmt, is_staged = await _ensure_accepted(audio_path)
            staged = audio if is_staged else None
            rel = _rel_under_storage(str(audio))
            audio_url = f"{base}/api/asr-source/{sign_source_token(rel)}"

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
        body = {}
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
