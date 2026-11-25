# -*- coding: utf-8 -*-
"""`main.py` CLI 接线回归：不导入主流程重依赖，仅抽取参数解析函数执行。"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

MAIN = Path(__file__).resolve().parents[2] / "main.py"


def _parse_arguments():
    """从 main.py 抽出 parse_arguments 并在最小命名空间里执行（避免 dotenv 等依赖）。"""
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "parse_arguments"
    )
    namespace: dict = {"argparse": argparse}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(MAIN), "exec"), namespace)
    return namespace["parse_arguments"]()


def test_assistant_flags_parse(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["main.py", "--assistant", "--assistant-once"])
    args = _parse_arguments()

    assert args.assistant is True
    assert args.assistant_once is True
    assert args.schedule is False  # 与主流程定时模式互不替代


def test_defaults_do_not_enable_assistant(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["main.py"])
    args = _parse_arguments()

    assert args.assistant is False
    assert args.assistant_once is False
