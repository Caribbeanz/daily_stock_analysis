# -*- coding: utf-8 -*-
"""
Auth middleware: protect /api/v1/* when admin auth is enabled.
"""

from __future__ import annotations

import logging
from typing import Callable

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from src.auth import COOKIE_NAME, is_auth_enabled, verify_session

logger = logging.getLogger(__name__)

EXEMPT_PATHS = frozenset({
    "/api/v1/auth/login",
    "/api/v1/auth/status",
    "/api/health",
    "/api/v1/health",
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
    # 大屏（P4 · FR-14.1.4）：浏览器全屏、**免登录常驻**，需在鉴权开启时仍可拉取。
    # 该端点**只读**（仅聚合本人自选股 / 持仓的行情与结论），不含任何写能力与密钥。
    "/api/v1/screen/snapshot",
})

# 前缀放行：移动 H5（M1）为**内网免登录只读页**，详情路径带股票代码，无法穷举。
# ``/api/v1/h5`` 下只注册了两个只读 GET 接口，无任何写能力，故按前缀放行是安全的。
EXEMPT_PREFIXES = (
    "/api/v1/h5/",
)


def _path_exempt(path: str) -> bool:
    """Check if path is exempt from auth."""
    normalized = path.rstrip("/") or "/"
    if normalized in EXEMPT_PATHS:
        return True
    return any(normalized.startswith(prefix) for prefix in EXEMPT_PREFIXES)


class AuthMiddleware(BaseHTTPMiddleware):
    """Require valid session for /api/v1/* when auth is enabled."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable,
    ):
        if not is_auth_enabled():
            return await call_next(request)

        path = request.url.path
        if _path_exempt(path):
            return await call_next(request)

        if not path.startswith("/api/v1/"):
            return await call_next(request)

        cookie_val = request.cookies.get(COOKIE_NAME)
        if not cookie_val or not verify_session(cookie_val):
            return JSONResponse(
                status_code=401,
                content={
                    "error": "unauthorized",
                    "message": "Login required",
                },
            )

        return await call_next(request)


def add_auth_middleware(app):
    """Add auth middleware to protect API routes.

    The middleware is always registered; whether auth is enforced is determined
    at request time by is_auth_enabled() so the decision stays consistent across
    any runtime configuration reload.
    """
    app.add_middleware(AuthMiddleware)
