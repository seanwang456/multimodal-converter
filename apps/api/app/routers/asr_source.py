"""GET /api/asr-source/{token} —— 供火山 ASR 从公网回拉音频的无状态签名端点。规格 §15。

token 由 worker 侧 `volcano_asr.sign_source_token` 签发（HMAC + 过期时间 + 相对路径），
本端 `verify_source_token` 校验后，复用 storage 的路径穿越防护定位文件并返回。
无需 Redis/DB；worker 与 api 共享同一 ASR_SOURCE_SECRET（或 VOLCANO_ASR_API_KEY）。
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import FileResponse

from app.errors import ConversionError, ErrorCode
from app.providers.volcano_asr import verify_source_token
from app.services.storage import _safe_join  # 复用 storage_root 路径穿越防护

router = APIRouter(prefix="/api/asr-source", tags=["asr-source"])

_CONTENT_TYPE = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg"}


@router.get("/{token}")
def serve(token: str) -> FileResponse:
    rel, ok = verify_source_token(token)
    if not ok:
        raise ConversionError(ErrorCode.FILE_EXPIRED, "音频下载链接无效或已过期")
    path = _safe_join(rel)  # 校验必须落在 storage_root 内（防路径穿越）
    if not path.exists():
        raise ConversionError(ErrorCode.NOT_FOUND, "音频不存在或已清理")
    media_type = _CONTENT_TYPE.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(str(path), media_type=media_type, filename=path.name)
