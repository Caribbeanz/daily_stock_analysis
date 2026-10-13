# -*- coding: utf-8 -*-
"""意图层：自然语言 → 结构化意图（P2.1 对话·读；写操作在 P2.2 接入 action_service）。"""

from src.agent.intent.resolver import Intent, IntentKind, resolve

__all__ = ["Intent", "IntentKind", "resolve"]
