# -*- coding: utf-8 -*-
"""
===================================
API 依赖注入模块
===================================

职责：
1. 提供数据库 Session 依赖
2. 提供配置依赖
3. 提供服务层依赖
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Generator

from fastapi import Request
from sqlalchemy.orm import Session

from src.storage import DatabaseManager
from src.config import get_config, Config

if TYPE_CHECKING:  # 仅类型检查期导入，运行时不拉起无关渠道 SDK
    from src.services.system_config_service import SystemConfigService
    from src.services.runtime_scheduler import RuntimeSchedulerService
    from src.services.agent_chat_session_service import AgentChatSessionService


def get_db() -> Generator[Session, None, None]:
    """
    获取数据库 Session 依赖
    
    使用 FastAPI 依赖注入机制，确保请求结束后自动关闭 Session
    
    Yields:
        Session: SQLAlchemy Session 对象
        
    Example:
        @router.get("/items")
        async def get_items(db: Session = Depends(get_db)):
            ...
    """
    db_manager = DatabaseManager.get_instance()
    session = db_manager.get_session()
    try:
        yield session
    finally:
        session.close()


def get_assistant_store(request: Request):
    """助手独立存储（与 L1 同库、独立表名），按应用生命周期复用一个实例。"""
    from src.assistant.data.storage import AssistantStore

    store = getattr(request.app.state, "assistant_store", None)
    if store is None:
        config = get_config()
        store = AssistantStore(config.database_path)
        store.create_all()  # 只追加 assistant_* 表，不改现有 stock_daily
        request.app.state.assistant_store = store
    return store


def get_assistant_config_service(request: Request):
    """参数/资金配置服务（配置 API 的唯一入口）。"""
    from src.assistant.config.service import AssistantConfigService

    service = getattr(request.app.state, "assistant_config_service", None)
    if service is None:
        service = AssistantConfigService(get_assistant_store(request))
        request.app.state.assistant_config_service = service
    return service


def get_config_dep() -> Config:
    """
    获取配置依赖
    
    Returns:
        Config: 配置单例对象
    """
    return get_config()


def get_database_manager() -> DatabaseManager:
    """
    获取数据库管理器依赖
    
    Returns:
        DatabaseManager: 数据库管理器单例对象
    """
    return DatabaseManager.get_instance()


def get_agent_chat_session_service() -> AgentChatSessionService:
    """Build an Agent Chat session service for the current database manager."""
    from src.services.agent_chat_session_service import AgentChatSessionService

    return AgentChatSessionService(DatabaseManager.get_instance())


def get_system_config_service(request: Request) -> SystemConfigService:
    """Get app-lifecycle shared SystemConfigService instance."""
    from src.services.system_config_service import SystemConfigService

    service = getattr(request.app.state, "system_config_service", None)
    if service is None:
        service = SystemConfigService()
        request.app.state.system_config_service = service
    return service


def get_runtime_scheduler_service(request: Request) -> RuntimeSchedulerService:
    """Get app-lifecycle shared RuntimeSchedulerService instance."""
    from src.services.runtime_scheduler import RuntimeSchedulerService

    service = getattr(request.app.state, "runtime_scheduler_service", None)
    if service is None:
        service = RuntimeSchedulerService()
        request.app.state.runtime_scheduler_service = service
    return service
