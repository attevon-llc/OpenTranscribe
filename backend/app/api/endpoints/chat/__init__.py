"""RAG chat API (issue #52).

Three routers, mounted under different prefixes by ``app.api.router``:
  - ``router``          → ``/chat``            (conversations + messages, capability-gated)
  - ``user_router``     → ``/user-settings``   (per-user chat preferences)
  - ``admin_router``    → ``/admin/chat-settings`` (platform RAG tuning)
"""

from fastapi import APIRouter

from app.api.endpoints.chat import admin_settings
from app.api.endpoints.chat import conversations
from app.api.endpoints.chat import export
from app.api.endpoints.chat import messages
from app.api.endpoints.chat import user_settings

router = APIRouter()
router.include_router(conversations.router)
router.include_router(messages.router)
router.include_router(export.router)

user_router = user_settings.router
admin_router = admin_settings.router

__all__ = ["admin_router", "router", "user_router"]
