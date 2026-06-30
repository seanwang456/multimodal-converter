"""ASR「回拉音频」模式的共享基础设施：无状态 HMAC 签名下载 token + 路径辅助。

任何采用"服务商从公网拉取音频"的 ASR provider（火山豆包、阿里云 filetrans 等）
都复用这里的 token 签发/校验与 `/api/asr-source/{token}` 端点（见 routers/asr_source.py）。
worker 与 api 共享同一 `ASR_SOURCE_SECRET`（缺省用各 provider 的 API key 派生）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
from pathlib import Path
from urllib.parse import urlparse

from app.config import settings
from app.errors import ConversionError, ErrorCode


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


def is_local_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "0.0.0.0", "::1", "")


def rel_under_storage(audio_path: str, storage_root: str | Path | None = None) -> str:
    """计算相对 storage_root 的 posix 路径；不在其下则拒绝（无法生成公网链接）。

    storage_root 可由调用方（provider）传入，便于使用各自已注入的 settings（测试可 patch）。
    """
    p = Path(audio_path).resolve()
    root = Path(storage_root).resolve() if storage_root else settings.storage_root.resolve()
    try:
        return p.relative_to(root).as_posix()
    except ValueError as exc:
        raise ConversionError(
            ErrorCode.ASR_FAILED, "音频不在存储目录内，无法生成公网下载链接"
        ) from exc


def require_public_base(public_base_url: str | None = None) -> str:
    """返回去尾斜杠的 PUBLIC_BASE_URL；为空或本地地址则抛 ASR_FAILED。

    public_base_url 可由调用方（provider）传入，便于使用各自已注入的 settings（测试可 patch）。
    """
    base = (public_base_url if public_base_url is not None else (settings.public_base_url or "")).rstrip("/")
    if not base or is_local_url(base):
        raise ConversionError(
            ErrorCode.ASR_FAILED,
            "ASR 需要公网可达的 PUBLIC_BASE_URL（当前为本地/空，服务商无法回拉音频）",
        )
    return base
