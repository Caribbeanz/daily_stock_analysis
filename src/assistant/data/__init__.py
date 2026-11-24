# -*- coding: utf-8 -*-
"""数据层（C2）：不复权取数、L1 聚合、L1–L4 存储。

**懒加载**：``storage`` 依赖 SQLAlchemy，而 ``fetcher`` 不需要。因此本包不 eager import
storage——取数脚本可以只装 akshare 就跑（冒烟脚本与部分单测依赖这一点）。
"""

from __future__ import annotations

from typing import Any

__all__ = ["AssistantStore", "AppendOnlyViolation", "DailyFetcher", "FetchError", "default_db_path"]


def __getattr__(name: str) -> Any:
    if name in ("AssistantStore", "AppendOnlyViolation", "default_db_path"):
        from src.assistant.data import storage

        return getattr(storage, name)
    if name in ("DailyFetcher", "FetchError"):
        from src.assistant.data import fetcher

        return getattr(fetcher, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
