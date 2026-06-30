"""统一 Conversion Registry。后端唯一可信源：源+目标→handler_key→handler。规格 §11、§7。"""
from __future__ import annotations

from typing import TypedDict

from app.errors import ConversionError, ErrorCode
from app.handlers.base import ConversionHandler


class ConversionRoute(TypedDict):
    target_ext: str
    handler_key: str
    quality: str


def _img_fmt(src: str) -> list[ConversionRoute]:
    """图片格式互转（Phase 2，§7.2.2）。"""
    table = {
        ".jpg": [".png", ".bmp"], ".jpeg": [".png", ".bmp"],
        ".png": [".jpg", ".bmp"], ".bmp": [".jpg", ".png"],
    }
    return [{"target_ext": t, "handler_key": "image_format_convert", "quality": "high"} for t in table[src]]


def _img_ocr() -> list[ConversionRoute]:
    """图片 OCR → 文档（Phase 3，§7.1.1）。"""
    return [
        {"target_ext": ".txt", "handler_key": "image_ocr_to_txt", "quality": "high"},
        {"target_ext": ".docx", "handler_key": "image_ocr_to_docx", "quality": "medium"},
        {"target_ext": ".pdf", "handler_key": "image_ocr_to_pdf", "quality": "medium"},
    ]


def _asr_routes() -> list[ConversionRoute]:
    """音频/视频 ASR → 文档（Phase 3，§7.1.2/7.1.3）。"""
    return [
        {"target_ext": ".txt", "handler_key": "audio_asr_to_txt", "quality": "high"},
        {"target_ext": ".docx", "handler_key": "audio_asr_to_docx", "quality": "high"},
        {"target_ext": ".pdf", "handler_key": "audio_asr_to_pdf", "quality": "high"},
    ]


def _video_asr_routes() -> list[ConversionRoute]:
    return [
        {"target_ext": ".txt", "handler_key": "video_asr_to_txt", "quality": "high"},
        {"target_ext": ".docx", "handler_key": "video_asr_to_docx", "quality": "high"},
        {"target_ext": ".pdf", "handler_key": "video_asr_to_pdf", "quality": "high"},
    ]


CONVERSION_REGISTRY: dict[str, list[ConversionRoute]] = {
    ".txt": [
        {"target_ext": ".docx", "handler_key": "txt_to_docx", "quality": "high"},
        {"target_ext": ".pdf", "handler_key": "txt_to_pdf", "quality": "high"},
        {"target_ext": ".pptx", "handler_key": "txt_to_pptx", "quality": "medium"},
        {"target_ext": ".xlsx", "handler_key": "txt_to_xlsx", "quality": "medium"},
    ],
    ".jpg": _img_fmt(".jpg") + _img_ocr(),
    ".jpeg": _img_fmt(".jpeg") + _img_ocr(),
    ".png": _img_fmt(".png") + _img_ocr(),
    ".bmp": _img_fmt(".bmp") + _img_ocr(),
    ".mp3": [{"target_ext": ".wav", "handler_key": "audio_format_convert", "quality": "high"}] + _asr_routes(),
    ".wav": [{"target_ext": ".mp3", "handler_key": "audio_format_convert", "quality": "high"}] + _asr_routes(),
    ".m4a": [
        {"target_ext": ".mp3", "handler_key": "audio_format_convert", "quality": "high"},
        {"target_ext": ".wav", "handler_key": "audio_format_convert", "quality": "high"},
    ] + _asr_routes(),
    ".aac": [
        {"target_ext": ".mp3", "handler_key": "audio_format_convert", "quality": "high"},
        {"target_ext": ".wav", "handler_key": "audio_format_convert", "quality": "high"},
    ] + _asr_routes(),
    ".mp4": _video_asr_routes(),
    ".pdf": [
        {"target_ext": ".txt", "handler_key": "pdf_to_txt", "quality": "high"},
        {"target_ext": ".docx", "handler_key": "pdf_to_docx", "quality": "best_effort"},
        {"target_ext": ".xlsx", "handler_key": "pdf_to_xlsx", "quality": "table_only"},
        {"target_ext": ".pptx", "handler_key": "pdf_to_pptx", "quality": "best_effort"},
    ],
    ".docx": [
        {"target_ext": ".pdf", "handler_key": "office_to_pdf", "quality": "high"},
        {"target_ext": ".txt", "handler_key": "office_to_txt", "quality": "high"},
        {"target_ext": ".pptx", "handler_key": "docx_to_pptx", "quality": "best_effort"},
    ],
    ".doc": [{"target_ext": ".pdf", "handler_key": "office_to_pdf", "quality": "best_effort"}],
    ".pptx": [
        {"target_ext": ".pdf", "handler_key": "office_to_pdf", "quality": "high"},
        {"target_ext": ".txt", "handler_key": "office_to_txt", "quality": "high"},
        {"target_ext": ".docx", "handler_key": "pptx_to_docx", "quality": "best_effort"},
    ],
    ".ppt": [{"target_ext": ".pdf", "handler_key": "office_to_pdf", "quality": "best_effort"}],
    ".xlsx": [
        {"target_ext": ".pdf", "handler_key": "office_to_pdf", "quality": "high"},
        {"target_ext": ".txt", "handler_key": "office_to_txt", "quality": "high"},
        {"target_ext": ".csv", "handler_key": "xlsx_to_csv", "quality": "high"},
    ],
    ".xls": [{"target_ext": ".pdf", "handler_key": "office_to_pdf", "quality": "best_effort"}],
}

_HANDLERS: dict[str, ConversionHandler] = {}


def register_handlers() -> None:
    """启动时注册全部真实 handler 实现。"""
    from app.handlers.audio_handlers import AUDIO_HANDLERS
    from app.handlers.image_handlers import IMAGE_HANDLERS
    from app.handlers.multimodal_handlers import MULTIMODAL_HANDLERS
    from app.handlers.office_handlers import OFFICE_HANDLERS
    from app.handlers.pdf_handlers import PDF_HANDLERS
    from app.handlers.txt_handlers import TXT_HANDLERS

    _HANDLERS.clear()
    _HANDLERS.update(TXT_HANDLERS)
    _HANDLERS.update(IMAGE_HANDLERS)
    _HANDLERS.update(AUDIO_HANDLERS)
    _HANDLERS.update(OFFICE_HANDLERS)
    _HANDLERS.update(PDF_HANDLERS)
    _HANDLERS.update(MULTIMODAL_HANDLERS)


def get_handler(handler_key: str) -> ConversionHandler | None:
    return _HANDLERS.get(handler_key)


def _implemented_routes(source_ext: str) -> list[ConversionRoute]:
    return [r for r in CONVERSION_REGISTRY.get(source_ext, []) if r["handler_key"] in _HANDLERS]


def get_targets(source_ext: str) -> list[ConversionRoute]:
    if source_ext not in CONVERSION_REGISTRY:
        raise ConversionError(ErrorCode.UNSUPPORTED_FILE_TYPE, "不支持该源格式")
    return _implemented_routes(source_ext)


def resolve_handler_key(source_ext: str, target_ext: str) -> str:
    routes = {r["target_ext"]: r for r in _implemented_routes(source_ext)}
    if target_ext not in routes:
        raise ConversionError(ErrorCode.UNSUPPORTED_CONVERSION, f"不支持 {source_ext} 转 {target_ext}")
    return routes[target_ext]["handler_key"]


def validate_conversion(source_ext: str, target_ext: str) -> str:
    return resolve_handler_key(source_ext, target_ext)


def self_check() -> None:
    problems: list[str] = []
    for src, routes in CONVERSION_REGISTRY.items():
        for r in routes:
            h = _HANDLERS.get(r["handler_key"])
            if h is None:
                problems.append(f"{src}->{r['target_ext']} 缺 handler {r['handler_key']}")
            elif not h.supports(src, r["target_ext"]):
                problems.append(f"{src}->{r['target_ext']} handler {r['handler_key']} supports() 返回 False")
    if problems:
        raise ConversionError(ErrorCode.CONVERSION_ENGINE_ERROR, "registry 自检失败：" + "; ".join(problems))
