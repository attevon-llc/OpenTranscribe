"""RAG chat services (issue #52).

Pipeline for one turn:
    scope → retrieve → rerank → mask → prompt → stream → persist

Each stage is its own module so the security-critical ones (``redactor`` and
``prompting``) can be read and tested without wading through streaming plumbing.
"""

from app.services.chat.context_resolver import count_scope_files
from app.services.chat.context_resolver import resolve_scope_file_uuids
from app.services.chat.retrieval import RetrievalResult
from app.services.chat.retrieval import retrieve_context
from app.services.chat.service import ChatService
from app.services.chat.service import sse
from app.services.chat.settings import ChatSettings
from app.services.chat.settings import get_chat_settings

__all__ = [
    "ChatService",
    "ChatSettings",
    "RetrievalResult",
    "count_scope_files",
    "get_chat_settings",
    "resolve_scope_file_uuids",
    "retrieve_context",
    "sse",
]
