"""Pre-generated chat conversations in the demo seed bundle (issue #628, P-D).

The demo never calls a live LLM (owner decision 2026-10-01): chat writes are refused, and
the demo user instead finds a few finished conversations in the sidebar. Their text is real
model output, produced once by ``scripts/demo/generate_demo_conversations.py`` driving the
app's own chat pipeline over the seeded content, never written by hand and never by the mock
LLM.

Bundle layout (all optional; a bundle without ``conversations`` seeds none)::

    manifest.json   "conversations": [{"id": "meeting-actions", "file": "conversations/x.json"}]
    conversations/x.json
        {"title": "...", "items": ["item-id", ...],
         "messages": [{"role": "user", "content": "..."},
                      {"role": "assistant", "content": "...", "provider": "vllm",
                       "model": "...", "citations": [{"item_id": "item-id",
                       "start_time": 12.0, "end_time": 20.5, "speaker": "Reporter",
                       "snippet": "...", "title": "...", "chunk_index": 3}]}]}

Citations name a bundle ``item_id``; the importer swaps it for the seeded file's UUID, so a
bundle stays independent of any database.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.scripts.demo_seed.bundle import BundleError


@dataclass(frozen=True)
class SeedConversation:
    """One validated conversation."""

    id: str
    title: str
    item_ids: list[str]
    messages: list[dict[str, Any]]


def _problems(conv_id: str, data: Any, item_ids: set[str]) -> list[str]:
    if not isinstance(data, dict):
        return [f"{conv_id}: not a JSON object"]
    problems: list[str] = []
    if not str(data.get("title") or "").strip():
        problems.append(f"{conv_id}: missing title")
    scope = data.get("items")
    if not isinstance(scope, list) or not scope:
        problems.append(f"{conv_id}: items must be a non-empty list")
        scope = []
    problems += [f"{conv_id}: unknown item {i!r}" for i in scope if i not in item_ids]
    messages = data.get("messages")
    if not isinstance(messages, list) or len(messages) < 2:
        return problems + [f"{conv_id}: needs at least one question and one answer"]
    for n, msg in enumerate(messages):
        expected = "user" if n % 2 == 0 else "assistant"
        if not isinstance(msg, dict) or msg.get("role") != expected:
            problems.append(f"{conv_id}: message {n} must be role {expected!r}")
            continue
        if not str(msg.get("content") or "").strip():
            problems.append(f"{conv_id}: message {n} has no content")
        if expected == "assistant" and not (msg.get("provider") and msg.get("model")):
            problems.append(f"{conv_id}: message {n} must record the provider and model")
        for cite in msg.get("citations") or []:
            if not isinstance(cite, dict) or cite.get("item_id") not in scope:
                problems.append(f"{conv_id}: message {n} cites an item outside the scope")
    if messages and isinstance(messages[-1], dict) and messages[-1].get("role") != "assistant":
        problems.append(f"{conv_id}: must end with an answer")
    return problems


def load_conversations(root: Path, item_ids: set[str]) -> list[SeedConversation]:
    """The bundle's conversations; :class:`BundleError` listing every defect otherwise."""
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    entries = manifest.get("conversations") or []
    problems: list[str] = []
    found: list[SeedConversation] = []
    for entry in entries:
        conv_id = str(entry.get("id") or "?") if isinstance(entry, dict) else "?"
        rel = entry.get("file") if isinstance(entry, dict) else None
        path = (root / rel).resolve() if isinstance(rel, str) and rel else None
        if path is None or not path.is_relative_to(root.resolve()) or not path.is_file():
            problems.append(f"{conv_id}: file missing or outside the bundle")
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            problems.append(f"{conv_id}: file is not valid JSON")
            continue
        found_problems = _problems(conv_id, data, item_ids)
        problems += found_problems
        if not found_problems:
            found.append(SeedConversation(conv_id, data["title"], data["items"], data["messages"]))
    if problems:
        raise BundleError(problems)
    return found
