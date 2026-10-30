"""Attributes and cross-module calls the ``LLMService`` mixins rely on.

The mixins (``llm_payloads``, ``llm_chunking``, ``llm_summarization``, ``llm_json_extract``,
``llm_speaker_id``, ``llm_health``) are mixed into :class:`~app.services.llm_service.LLMService`,
which sets these attributes in ``__init__`` and owns the transport methods. Declaring them here
lets the type checker verify each mixin against the same contract; nothing in this class exists
at runtime beyond the annotations.
"""

from typing import TYPE_CHECKING

from app.services.llm_types import LLMConfig
from app.services.llm_types import LLMProvider
from app.services.llm_types import LLMResponse


class LLMHost:
    """Contract a mixin may assume of the ``LLMService`` it is mixed into."""

    config: LLMConfig
    user_context_window: int
    response_tokens: int
    endpoints: dict[LLMProvider, str | None]

    if TYPE_CHECKING:

        def chat_completion(self, messages: list[dict[str, str]], **kwargs) -> LLMResponse: ...

        def _get_headers(self) -> dict[str, str]: ...
