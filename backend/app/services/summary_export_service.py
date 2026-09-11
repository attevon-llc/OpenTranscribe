"""Server-side summary export serialization (markdown only).

Issue #885 was filed as a redaction bypass on the summary copy button and its premise was
wrong: ``GET /api/files/{uuid}/summary`` already masks via ``_redacted_summary`` /
``mask_summary`` (``redaction/summary_redaction.py``, issue #465), and this module never
re-implements that — it takes an already-masked ``dict`` and only formats it.

The REAL defects, both in ``frontend/src/components/SummaryModal.svelte``:

1. Its client-side ``formatSummaryAsMarkdown`` dropped the ``action_items`` and
   ``speakers_analysis`` sections entirely — ``SummaryDisplay.svelte`` renders both, the
   serializer did not. Ported here with both sections restored.
2. It was the **last** client-side re-serialization of server data in the SPA (every other
   copy/export surface was converted by #673/#821 — see ``frontend/src/components/CLAUDE.md``'s
   "every copyable byte comes from a server export endpoint" rule). A client-side serializer
   cannot consult any future server-side policy (e.g. a hypothetical ``?redact=false`` reveal
   mirroring the transcript's "show original" toggle) — it would silently inherit whatever the
   summary read endpoint already resolved into the JSON it was handed, one layer removed from
   the actual policy decision.

⚠️ **This module takes a plain ``dict`` and never touches the DB/ORM.** It must be handed the
ALREADY-MASKED summary (the return value of ``_redacted_summary`` /
``redaction.summary_redaction.mask_summary``), never ``media_file.summary_data`` directly — see
``backend/app/services/CLAUDE.md``'s ORM-mutation/read-time-masking gotcha for why a masker
must never write back to a loaded ORM object, and ``redaction/summary_redaction.py`` for why a
summary cannot use cached transcript-segment spans and must mask every string leaf itself.

Modeled directly on ``transcript_export_service.py`` (issue #673's precedent): a pure function
of already-resolved data plus caller-supplied i18n label strings, so this module stays
translation-free like the rest of the backend.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

VALID_SUMMARY_EXPORT_FORMATS = ("md",)

#: Matches `frontend/src/components/SummaryModal.svelte`'s retired `removeEmojis` regex
#: (`/[\u{1F600}-\u{1F64F}]|[\u{1F300}-\u{1F5FF}]|[\u{1F680}-\u{1F6FF}]|[\u{1F1E0}-\u{1F1FF}]|
#: [\u{2600}-\u{26FF}]|[\u{2700}-\u{27BF}]/gu`), so removing that client-side function does not
#: change what a copied/exported summary looks like.
_EMOJI_PATTERN = re.compile(
    "["
    "\U0001f600-\U0001f64f"
    "\U0001f300-\U0001f5ff"
    "\U0001f680-\U0001f6ff"
    "\U0001f1e0-\U0001f1ff"
    "\U00002600-\U000026ff"
    "\U00002700-\U000027bf"
    "]+"
)


@dataclass(frozen=True)
class SummaryExportLabels:
    """Resolved i18n strings the caller supplies; this module renders them verbatim.

    ``key_participants`` carries a literal ``"{participants}"`` placeholder, substituted here
    with the (already-masked) participant names — mirrors the retired
    ``$t('summary.keyParticipants', { participants: ... })`` call. ``disclaimer`` is fully
    resolved by the caller (provider/model/processing-time already interpolated), unlike every
    other field here which is a bare label.
    """

    title: str
    executive_summary: str
    brief_summary: str
    major_topics: str
    key_participants: str
    importance_high: str
    importance_medium: str
    importance_low: str
    action_items: str
    owner: str
    due_date: str
    key_decisions: str
    speaker_analysis: str
    follow_up_items: str
    disclaimer: str


def _strip_emoji(text: Any) -> str:
    """Port of the retired client-side ``removeEmojis``. ``None``/falsy -> ``""``."""
    if not text:
        return ""
    return _EMOJI_PATTERN.sub("", str(text)).strip()


def _is_standard_bluf(data: dict[str, Any]) -> bool:
    """Port of ``!!(data.bluf && data.brief_summary)`` — a custom prompt lacking either
    field renders through the flexible ``_render_custom`` path instead."""
    return bool(data.get("bluf") and data.get("brief_summary"))


def _extract_text(value: Any) -> str:
    """Port of ``extractText`` — pulls display text out of a decision/follow-up entry that
    may be a bare string or an object shaped by a custom prompt."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        text = (
            value.get("decision")
            or value.get("text")
            or value.get("item")
            or value.get("description")
        )
        if text:
            return str(text)
        return json.dumps(value, ensure_ascii=False)
    return str(value)


# --------------------------------------------------------------------------------------- #
# Action items — the shape-tolerant fallback chain from `SummaryDisplay.svelte`'s
# `actionItemText`/`actionItemOwner`/`actionItemDueDate`/`actionItemPriority`. This is one of
# the two sections the retired client serializer never rendered at all (the bug fix).
# --------------------------------------------------------------------------------------- #


def _action_item_text(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return str(item.get("item") or item.get("text") or item.get("description") or "")
    return ""


def _action_item_owner(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("owner") or item.get("assigned_to") or "")
    return ""


def _action_item_due_date(item: Any) -> str:
    if isinstance(item, dict) and item.get("due_date"):
        return str(item["due_date"])
    return ""


def _action_item_priority(item: Any) -> str:
    if isinstance(item, dict) and item.get("priority"):
        return str(item["priority"])
    return ""


# --------------------------------------------------------------------------------------- #
# Speaker analysis — `SummaryDisplay.svelte`'s `speakerEntries`/`speakerName`/`speakerRole`/
# `speakerTalkTime`/`speakerPoints`. The second section the retired serializer never rendered.
# --------------------------------------------------------------------------------------- #


def _speaker_entries(data: dict[str, Any]) -> list[Any]:
    return data.get("speakers_analysis") or data.get("speakers") or []


def _speaker_name(entry: Any) -> str:
    if isinstance(entry, dict):
        return str(entry.get("speaker") or entry.get("name") or "")
    return ""


def _speaker_role(entry: Any) -> str:
    if isinstance(entry, dict):
        return str(entry.get("role") or "")
    return ""


def _speaker_talk_time(entry: Any) -> Any:
    if not isinstance(entry, dict):
        return None
    pct = entry.get("talk_time_percentage")
    if pct is None:
        pct = entry.get("percentage")
    return pct


def _speaker_points(entry: Any) -> list[Any]:
    if isinstance(entry, dict):
        return entry.get("key_contributions") or entry.get("key_points") or []
    return []


def _render_major_topics_section(data: dict[str, Any], labels: SummaryExportLabels) -> str:
    major_topics = data.get("major_topics") or []
    if not major_topics:
        return ""
    out = f"## {labels.major_topics}\n"
    importance_labels = {"high": labels.importance_high, "medium": labels.importance_medium}
    for topic in major_topics:
        if not isinstance(topic, dict):
            continue
        importance = topic.get("importance")
        importance_label = (
            importance_labels.get(importance, labels.importance_low)
            if isinstance(importance, str)
            else labels.importance_low
        )
        out += f"### [{importance_label}] {_strip_emoji(topic.get('topic', ''))}\n"
        participants = topic.get("participants") or []
        if participants:
            joined = ", ".join(str(p) for p in participants)
            out += f"*{labels.key_participants.replace('{participants}', joined)}*\n\n"
        for point in topic.get("key_points") or []:
            out += f"- {_strip_emoji(point)}\n"
        out += "\n"
    return out


def _render_action_items_section(data: dict[str, Any], labels: SummaryExportLabels) -> str:
    """One of the two sections the retired client serializer never rendered — the bug fix."""
    action_items = data.get("action_items") or []
    if not action_items:
        return ""
    out = f"## {labels.action_items}\n"
    for item in action_items:
        line = f"- {_strip_emoji(_action_item_text(item))}"
        meta_parts = []
        owner = _action_item_owner(item)
        if owner:
            meta_parts.append(f"{labels.owner}: {_strip_emoji(owner)}")
        due_date = _action_item_due_date(item)
        if due_date:
            meta_parts.append(f"{labels.due_date}: {due_date}")
        priority = _action_item_priority(item)
        if priority:
            meta_parts.append(str(priority).upper())
        if meta_parts:
            line += f" ({'; '.join(meta_parts)})"
        out += line + "\n"
    return out + "\n"


def _render_key_decisions_section(data: dict[str, Any], labels: SummaryExportLabels) -> str:
    key_decisions = data.get("key_decisions") or []
    if not key_decisions:
        return ""
    out = f"## {labels.key_decisions}\n"
    for decision in key_decisions:
        text = decision if isinstance(decision, str) else _extract_text(decision)
        out += f"- {_strip_emoji(text)}\n"
    return out + "\n"


def _render_speaker_analysis_section(data: dict[str, Any], labels: SummaryExportLabels) -> str:
    """The other section the retired client serializer never rendered — the bug fix."""
    speaker_entries = _speaker_entries(data)
    if not speaker_entries:
        return ""
    out = f"## {labels.speaker_analysis}\n"
    for entry in speaker_entries:
        name = _strip_emoji(_speaker_name(entry))
        header = f"### {name}" if name else "###"
        meta = []
        role = _strip_emoji(_speaker_role(entry))
        if role:
            meta.append(role)
        talk_time = _speaker_talk_time(entry)
        if talk_time is not None:
            meta.append(f"{talk_time}%")
        if meta:
            header += f" ({', '.join(meta)})"
        out += header + "\n"
        for point in _speaker_points(entry):
            out += f"- {_strip_emoji(point)}\n"
        out += "\n"
    return out


def _render_follow_up_section(data: dict[str, Any], labels: SummaryExportLabels) -> str:
    follow_up_items = data.get("follow_up_items") or []
    if not follow_up_items:
        return ""
    out = f"## {labels.follow_up_items}\n"
    for item in follow_up_items:
        text = item if isinstance(item, str) else _extract_text(item)
        out += f"- {_strip_emoji(text)}\n"
    return out + "\n"


def _render_standard_bluf(data: dict[str, Any], labels: SummaryExportLabels) -> str:
    """BLUF -> brief summary -> major topics -> action items -> key decisions ->
    speaker analysis -> follow-up items. Action items and speaker analysis are the bug fix
    (see the two section renderers' docstrings); this order matches ``SummaryDisplay.svelte``'s
    rendered markup order exactly."""
    out = ""

    bluf = data.get("bluf")
    if bluf:
        out += f"## {labels.executive_summary}\n{_strip_emoji(bluf)}\n\n"

    brief_summary = data.get("brief_summary")
    if brief_summary:
        out += f"## {labels.brief_summary}\n{_strip_emoji(brief_summary)}\n\n"

    out += _render_major_topics_section(data, labels)
    out += _render_action_items_section(data, labels)
    out += _render_key_decisions_section(data, labels)
    out += _render_speaker_analysis_section(data, labels)
    out += _render_follow_up_section(data, labels)

    return out


def _title_case_key(key: str) -> str:
    """Snake/space-separated key -> Title Case.

    Matches the retired ``formatCustomSummaryMarkdown``'s
    ``key.replace(/_/g, ' ').replace(/\\b\\w/g, l => l.toUpperCase())`` — only the FIRST
    character of each word is uppercased; unlike ``str.title()`` the rest of each word is left
    untouched, so an already-mixed-case key is not mangled.
    """
    words = key.replace("_", " ").split(" ")
    return " ".join((w[0].upper() + w[1:]) if w else w for w in words)


def _render_custom(obj: dict[str, Any], heading_level: int = 2) -> str:
    """Port of the retired ``formatCustomSummaryMarkdown`` — unchanged behavior, for any
    summary shape a custom prompt produced that isn't the standard BLUF format."""
    out = ""
    heading_prefix = "#" * heading_level

    for key, value in obj.items():
        if key == "metadata":
            continue
        out += f"{heading_prefix} {_title_case_key(key)}\n"

        if value is None:
            out += "*No data*\n\n"
        elif isinstance(value, str):
            out += f"{_strip_emoji(value)}\n\n"
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    out += f"- {_strip_emoji(item)}\n"
                elif isinstance(item, dict):
                    text = (
                        item.get("text")
                        or item.get("decision")
                        or item.get("item")
                        or item.get("description")
                        or json.dumps(item, ensure_ascii=False)
                    )
                    out += f"- {_strip_emoji(text)}\n"
                else:
                    out += f"- {item}\n"
            out += "\n"
        elif isinstance(value, dict):
            out += _render_custom(value, heading_level + 1)
        else:
            out += f"{value}\n\n"

    return out


def build_summary_export(
    summary_data: dict[str, Any], *, export_format: str, labels: SummaryExportLabels
) -> str:
    """Serialize an already-masked summary into ``export_format``.

    Args:
        summary_data: The result of masking ``media_file.summary_data`` for the requesting
            user (``_redacted_summary`` in ``api/endpoints/summarization.py``). NEVER pass the
            raw stored column — this function applies no redaction of its own.
        export_format: One of :data:`VALID_SUMMARY_EXPORT_FORMATS`.
        labels: Resolved i18n strings this module renders verbatim (translation-free backend).

    Returns:
        The rendered document as a string.

    Raises:
        ValueError: ``export_format`` is not supported.
    """
    if export_format not in VALID_SUMMARY_EXPORT_FORMATS:
        raise ValueError(f"Unsupported export format: {export_format}")

    markdown = f"# {labels.title}\n\n"

    if _is_standard_bluf(summary_data):
        markdown += _render_standard_bluf(summary_data, labels)
    else:
        markdown += _render_custom(summary_data, 2)

    if summary_data.get("metadata"):
        markdown += f"---\n\n*{labels.disclaimer}*\n"

    return markdown
