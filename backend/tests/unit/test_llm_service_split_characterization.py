"""``LLMService`` keeps exactly the surface it had before the transport/factory split (L07).

The split moves methods verbatim into ``LLMTransportMixin`` / ``LLMFactoryMixin``; Python
resolves them through the MRO, so every name stays reachable on ``LLMService`` with the same
kind (static / instance / property). The fixture was captured on the pre-split HEAD.
"""

import inspect
import json
import re
from pathlib import Path

from app.services import llm_service
from app.services.llm_service import LLMService

FIXTURE = Path(__file__).parent / "fixtures" / "llm_service_surface_head.json"


def surface() -> dict[str, str]:
    out = {}
    for name in dir(LLMService):
        if name.startswith("__") and name not in ("__init__", "__enter__", "__exit__"):
            continue
        raw = inspect.getattr_static(LLMService, name)
        if isinstance(raw, staticmethod):
            kind = "staticmethod"
        elif isinstance(raw, property):
            kind = "property"
        elif callable(raw):
            kind = "function"
        else:
            kind = "attribute"
        out[name] = kind
    return out


def test_method_set_and_kinds_equal_head():
    assert surface() == json.loads(FIXTURE.read_text())["surface"]


def test_public_module_names_equal_head():
    head = json.loads(FIXTURE.read_text())["module_names"]
    missing = [n for n in head if not hasattr(llm_service, n)]
    assert missing == []


def test_head_surface_is_not_vacuous():
    head = json.loads(FIXTURE.read_text())["surface"]
    assert head["create_from_config_id"] == "staticmethod"
    assert head["_send_llm_request"] == "function"
    assert head["chat_completion"] == "function"
    assert len(head) > 40


def test_signatures_of_moved_methods_unchanged():
    head = json.loads(FIXTURE.read_text())["signatures"]

    # The private ``_ResolvedUserLLM`` return type moved modules; compare modulo module path.
    def norm(sig: str) -> str:
        return re.sub(r"app\.services\.llm_\w+\.", "", sig)

    now = {n: norm(str(inspect.signature(getattr(LLMService, n)))) for n in head}
    assert now == {n: norm(v) for n, v in head.items()}
