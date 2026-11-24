# -*- coding: utf-8 -*-
"""股票分析助手（assistant）——独立扩展模块。

与 ``src/agent/``（主流程 Agent/Skill）平级、互不依赖：
本包只做「确定性规则 + 盘前快照」，不复用主流程的 LLM 决策结论。

对应文档：
- 需求：``docs/requirements/stock-analysis-assistant-requirements-v2.6.md``
- 方案：``docs/technical-design/stock-analysis-assistant-technical-design-v0.3.md``
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
