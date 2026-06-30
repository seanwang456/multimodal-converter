"""统一错误码与 FastAPI 异常处理。规格 §16。返回 {error: {code, message}}。"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class ErrorCode:
    UNSUPPORTED_FILE_TYPE = "UNSUPPORTED_FILE_TYPE"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    EMPTY_FILE = "EMPTY_FILE"
    UNSUPPORTED_CONVERSION = "UNSUPPORTED_CONVERSION"
    PASSWORD_PROTECTED_PDF = "PASSWORD_PROTECTED_PDF"
    OCR_FAILED = "OCR_FAILED"
    ASR_FAILED = "ASR_FAILED"
    NO_AUDIO_TRACK = "NO_AUDIO_TRACK"
    NO_TABLE_FOUND = "NO_TABLE_FOUND"
    CONVERSION_TIMEOUT = "CONVERSION_TIMEOUT"
    CONVERSION_ENGINE_ERROR = "CONVERSION_ENGINE_ERROR"
    FILE_EXPIRED = "FILE_EXPIRED"
    DOWNLOAD_NOT_READY = "DOWNLOAD_NOT_READY"
    NOT_FOUND = "NOT_FOUND"


# 错误码 → HTTP 状态
ERROR_HTTP_STATUS: dict[str, int] = {
    ErrorCode.UNSUPPORTED_FILE_TYPE: 400,
    ErrorCode.FILE_TOO_LARGE: 413,
    ErrorCode.EMPTY_FILE: 400,
    ErrorCode.UNSUPPORTED_CONVERSION: 400,
    ErrorCode.PASSWORD_PROTECTED_PDF: 400,
    ErrorCode.NO_AUDIO_TRACK: 422,
    ErrorCode.NO_TABLE_FOUND: 422,
    ErrorCode.OCR_FAILED: 502,
    ErrorCode.ASR_FAILED: 502,
    ErrorCode.CONVERSION_TIMEOUT: 504,
    ErrorCode.CONVERSION_ENGINE_ERROR: 500,
    ErrorCode.FILE_EXPIRED: 410,
    ErrorCode.DOWNLOAD_NOT_READY: 409,
    ErrorCode.NOT_FOUND: 404,
}


class ConversionError(Exception):
    """携带结构化错误码的业务异常。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def error_response(code: str, message: str, status: int | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status or ERROR_HTTP_STATUS.get(code, 500),
        content={"error": {"code": code, "message": message}},
    )


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ConversionError)
    async def _conversion_error(_: Request, exc: ConversionError) -> JSONResponse:
        # 错误信息不得暴露服务器路径
        return error_response(exc.code, exc.message)

    @app.exception_handler(404)
    async def _not_found(_: Request, __: Exception) -> JSONResponse:
        return error_response(ErrorCode.NOT_FOUND, "资源不存在")
