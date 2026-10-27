"""网页层的结构化中文错误。

统一响应体：``{"error": {"code": ..., "message": ..., "details": ...}}``，
并使用真实的 HTTP 状态码——不用 200 包装失败。
错误信息里不出现密钥、请求头或服务端原始响应体。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from litworkbench.errors import (
    EditConflictError,
    LitError,
    NotFoundError,
    QuotationError,
    UnsupportedFileError,
    ValidationError,
    WorkspaceError,
)

#: 领域错误 -> (HTTP 状态, 机器可读代码)
ERROR_MAP: tuple[tuple[type[LitError], int, str], ...] = (
    (NotFoundError, 404, "NOT_FOUND"),
    (EditConflictError, 409, "EDIT_CONFLICT"),
    (QuotationError, 422, "QUOTATION_INVALID"),
    (UnsupportedFileError, 400, "UNSUPPORTED_FILE"),
    (ValidationError, 422, "VALIDATION_ERROR"),
    (WorkspaceError, 400, "WORKSPACE_ERROR"),
)

DEFAULT_STATUS = 400
DEFAULT_CODE = "BAD_REQUEST"

_STATUS_CODES = {
    400: "BAD_REQUEST",
    401: "SESSION_REQUIRED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    413: "FILE_TOO_LARGE",
    415: "UNSUPPORTED_MEDIA_TYPE",
    422: "VALIDATION_ERROR",
    500: "INTERNAL_ERROR",
    503: "SERVICE_UNAVAILABLE",
}


def error_body(code: str, message: str, details: Any = None) -> dict[str, Any]:
    body: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        body["details"] = details
    return {"error": body}


def error_response(status: int, code: str, message: str,
                   details: Any = None) -> JSONResponse:
    return JSONResponse(status_code=status, content=error_body(code, message, details))


def classify(exc: LitError) -> tuple[int, str]:
    # 领域错误可以带稳定的机器码/状态（例如 REVISION_CONFLICT、FILE_TOO_LARGE）。
    explicit_code = getattr(exc, "code", None)
    explicit_status = getattr(exc, "status_code", None)
    if isinstance(explicit_code, str) and type(explicit_status) is int:
        return explicit_status, explicit_code
    for error_type, status, code in ERROR_MAP:
        if isinstance(exc, error_type):
            return status, code
    return DEFAULT_STATUS, DEFAULT_CODE


def register_error_handlers(app: FastAPI) -> None:
    """把领域错误、FastAPI 校验错误和未捕获异常都转成同一结构。"""

    @app.exception_handler(LitError)
    async def _lit_error(_request: Request, exc: LitError) -> JSONResponse:
        status, code = classify(exc)
        return error_response(status, code, str(exc))

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _STATUS_CODES.get(exc.status_code, "HTTP_ERROR")
        detail = exc.detail if isinstance(exc.detail, str) else "请求无法处理。"
        return error_response(exc.status_code, code, detail)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [
            {
                "field": ".".join(str(part) for part in error.get("loc", [])[1:]) or "body",
                "message": error.get("msg", ""),
            }
            for error in exc.errors()[:10]
        ]
        message = "请求参数不合法：" + "；".join(
            f"{item['field']} {item['message']}".strip() for item in fields
        ) if fields else "请求参数不合法。"
        return error_response(422, "VALIDATION_ERROR", message, {"fields": fields})

    @app.exception_handler(sqlite3.IntegrityError)
    async def _integrity_error(_request: Request, exc: sqlite3.IntegrityError) -> JSONResponse:
        # 不把 SQL 或表结构暴露给浏览器，但也不假装成功。
        return error_response(409, "CONFLICT", f"数据写入被拒绝：{exc}")

    @app.exception_handler(Exception)
    async def _unexpected(_request: Request, exc: Exception) -> JSONResponse:
        return error_response(500, "INTERNAL_ERROR",
                              f"服务内部错误：{type(exc).__name__}。请查看运行窗口的日志。")