"""Give a stored conversation's speakers their names instead of ``SPEAKER_NN`` (issue #628).

The conversations were generated while the seeded transcripts carried no speaker names, so
the model quoted the raw diarization labels. Regenerating needs a real model; this rewrites
the stored text deterministically instead, and the generator applies it to what it writes.
Standard library only, so the generator script can load it by path.

A label means a different person in every recording, so a name is used only when it is
unambiguous: every recording the message cites (its citations' ``item_id``; the
conversation's items when it cites none) must name that label the same way. Otherwise the
generic ``Speaker NN`` the app itself shows for an unnamed speaker
(``speaker.localizedLabel`` in the English locale) is used, so a raw label never survives.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_LABEL = re.compile(r"\bSPEAKER_(\d+)\b")

#: item id -> {raw label: display name}
Names = dict[str, dict[str, str]]


def _name_for(label: str, candidates: list[str], names: Names) -> str:
    found = {names.get(i, {}).get(label) for i in dict.fromkeys(candidates)}
    if len(found) == 1 and None not in found:
        return str(next(iter(found)))
    return f"Speaker {label.removeprefix('SPEAKER_')}"


def relabel_text(text: str, candidates: list[str], names: Names) -> str:
    """``text`` with every raw label replaced; ``candidates`` are the items it may refer to."""
    return _LABEL.sub(lambda m: _name_for(m.group(0), candidates, names), text)


def relabel_conversation(conv: dict[str, Any], names: Names) -> dict[str, Any]:
    """A copy of a bundle conversation with message text and citation speakers relabelled."""
    scope = [str(i) for i in conv.get("items", [])]
    messages: list[dict[str, Any]] = []
    for msg in conv.get("messages", []):
        cited = [str(c["item_id"]) for c in msg.get("citations") or [] if c.get("item_id")]
        candidates = list(dict.fromkeys(cited)) or scope
        out = dict(msg)
        for key in ("content", "reasoning_content"):
            if isinstance(msg.get(key), str):
                out[key] = relabel_text(msg[key], candidates, names)
        if msg.get("citations"):
            out["citations"] = [
                {
                    **c,
                    "speaker": relabel_text(c["speaker"], [str(c.get("item_id"))], names)
                    if isinstance(c.get("speaker"), str)
                    else c.get("speaker"),
                }
                for c in msg["citations"]
            ]
        messages.append(out)
    return {**conv, "messages": messages}


def load_names(root: Path, extra: Path | None = None) -> Names:
    """Each item's label -> name map: its transcript's ``speakers``, then ``extra`` over it.

    ``extra`` is a JSON file shaped ``{item id: {label: name}}``, for names the transcripts
    do not carry yet.
    """
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    names: Names = {}
    for item in manifest.get("items", []):
        data = json.loads((root / item["transcript"]).read_text(encoding="utf-8"))
        names[item["id"]] = {str(k): str(v) for k, v in (data.get("speakers") or {}).items()}
    if extra is not None:
        for item_id, mapping in json.loads(extra.read_text(encoding="utf-8")).items():
            names.setdefault(item_id, {}).update(mapping)
    return names


def relabel_bundle(root: Path, out: Path, names: Names) -> int:
    """Write ``manifest.json`` and the relabelled ``conversations/`` to ``out``; the count.

    ``root`` is never modified, and ``out`` must not exist yet.
    """
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    (out / "conversations").mkdir(parents=True)
    count = 0
    for entry in manifest.get("conversations") or []:
        conv = json.loads((root / entry["file"]).read_text(encoding="utf-8"))
        (out / entry["file"]).write_text(
            json.dumps(relabel_conversation(conv, names), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        count += 1
    (out / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return count
