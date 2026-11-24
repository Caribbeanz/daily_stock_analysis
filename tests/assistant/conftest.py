# -*- coding: utf-8 -*-
"""assistant 模块测试的环境准备。

只做两件事：把仓库根加入 ``sys.path``，并保证测试**不加载** ``tests/conftest.py``
（后者依赖 fastapi/anyio 等全量依赖，本模块的引擎测试是纯逻辑、应可独立运行）。

运行方式：
    python -m pytest tests/assistant --confcutdir=tests/assistant -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def pytest_collection_modifyitems(config, items) -> None:
    """本目录全部为纯逻辑单测（无网络、无外部服务），统一打 ``unit`` 标记。"""
    marker = pytest.mark.unit
    for item in items:
        item.add_marker(marker)
