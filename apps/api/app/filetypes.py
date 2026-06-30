"""文件类型约束：扩展名→分类、期望 MIME、大小上限。规格 §6。"""
from __future__ import annotations

from app.config import settings

# 扩展名 → 分类
EXT_CATEGORY: dict[str, str] = {
    ".pdf": "pdf",
    ".docx": "word", ".doc": "word",
    ".pptx": "ppt", ".ppt": "ppt",
    ".xlsx": "excel", ".xls": "excel",
    ".txt": "txt",
    ".jpg": "image", ".jpeg": "image", ".png": "image", ".bmp": "image",
    ".mp3": "audio", ".wav": "audio", ".aac": "audio", ".m4a": "audio",
    ".mp4": "video",
}

# 期望 MIME（用于"扩展名与 MIME 不一致 = 拒绝"校验，规格 §22.2）
EXT_MIME: dict[str, str] = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".ppt": "application/vnd.ms-powerpoint",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".txt": "text/plain",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".bmp": "image/bmp",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".aac": "audio/aac",
    ".m4a": "audio/mp4",
    ".mp4": "video/mp4",
}

SUPPORTED_EXTS: set[str] = set(EXT_CATEGORY.keys())

# 每个扩展名允许的 MIME 集合（含常见合法变体）；不在集合内即视为不一致并拒绝
EXT_ALLOWED_MIMES: dict[str, set[str]] = {
    ".pdf": {"application/pdf"},
    ".docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    ".doc": {"application/msword"},
    ".pptx": {"application/vnd.openxmlformats-officedocument.presentationml.presentation"},
    ".ppt": {"application/vnd.ms-powerpoint"},
    ".xlsx": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
    ".xls": {"application/vnd.ms-excel"},
    ".txt": {"text/plain"},
    ".jpg": {"image/jpeg"}, ".jpeg": {"image/jpeg"},
    ".png": {"image/png"},
    ".bmp": {"image/bmp", "image/x-ms-bmp", "image/x-bitmap"},
    ".mp3": {"audio/mpeg", "audio/mp3"},
    ".wav": {"audio/wav", "audio/x-wav", "audio/wave"},
    ".aac": {"audio/aac", "audio/x-aac", "audio/aacp"},
    ".m4a": {"audio/mp4", "audio/x-m4a", "audio/m4a", "video/mp4"},  # m4a 本质是 MP4 容器，curl/部分客户端报 video/mp4
    ".mp4": {"video/mp4"},
}


def normalize_ext(filename: str) -> str:
    """返回小写带点的扩展名，如 'demo.PDF' -> '.pdf'。"""
    idx = filename.rfind(".")
    if idx < 0:
        return ""
    return filename[idx:].lower()


def category_of(source_ext: str) -> str | None:
    return EXT_CATEGORY.get(source_ext.lower())


def max_bytes_for(source_ext: str) -> int | None:
    cat = category_of(source_ext)
    if cat is None:
        return None
    # wav 单独走更大的上限
    mb = settings.max_wav_mb if source_ext.lower() == ".wav" else settings.category_max_mb[cat]
    return mb * 1024 * 1024


# 通用/未知类 MIME：不与任何扩展名构成矛盾，一律放行。
# 客户端（curl/部分浏览器/移动端）与 libmagic 对 OOXML 等 ZIP 容器常报 octet-stream/zip，
# 这类声明既不声称具体类型，也就无所谓"与扩展名不一致"；真正的内容校验交给下游
# 转换器（LibreOffice/ffmpeg/PIL）按字节解析兜底。
_GENERIC_MIMES = {
    "application/octet-stream",
    "application/zip",
    "application/x-zip-compressed",
}


def mime_consistent(uploaded_mime: str, source_ext: str) -> bool:
    """扩展名与显式 MIME 是否一致（必须在扩展名允许集合内）；客户端未带 MIME 时不拦截。"""
    allowed = EXT_ALLOWED_MIMES.get(source_ext.lower())
    if not allowed or not uploaded_mime:  # 未知扩展或缺 MIME：不在此层拦截
        return True
    base = uploaded_mime.split(";", 1)[0].strip().lower()
    if base in _GENERIC_MIMES:  # 通用/未知声明：不构成矛盾，放行
        return True
    return base in allowed
