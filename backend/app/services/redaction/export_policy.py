"""Whose redaction policy governs an EXPORT, and whether the export may be produced.

Three read surfaces produce a file the user keeps: the single-file subtitle download
(``GET /files/{uuid}/subtitles``), the bulk-export ZIP
(``POST /files/bulk-export/prepare`` → ``download.prepare_bulk_subtitles``), and the
burned-in-subtitle video (``download.prepare_media`` → ``VideoProcessingService``).
The first has resolved a policy since #0eecd839; the other two are Celery tasks and
resolved **nothing** — they called the subtitle generators with no config at all, which
``_redact_segments_inplace`` reads as "redaction is off", so they exported the raw
transcript **including under the admin ``redaction.force_export_redacted`` floor**
(issue #85). The floor's UI says exports are censored; two of the three export paths
ignored it.

**The subject is the REQUESTING USER, not the file owner.**

A bulk export is one reader downloading a set that may span several owners, so there is
no single owner to ask — the same argument that makes ``chat/redactor`` resolve the
requesting user. More decisively, the single-file endpoint beside it already resolves
``resolve_effective_config(db, current_user.id)``: had the batch used the owner instead,
the two buttons would apply different policies to the same file and the *weaker* of them
would become the real policy for anyone who knew which one to press. What that
alternative would have leaked is concrete — a reader whose own policy masks PII, on a
file whose owner disabled redaction, would receive an unmasked ZIP that the single-file
download and the transcript page both refuse to give them.

``llm_guard`` resolving the FILE OWNER is not a counter-example: it governs **egress to a
third-party provider**, which is the owner's data-protection decision about their own
content. An export is delivered to someone the permission layer has already authorized
to read the transcript, so it is a read surface, and read surfaces here are keyed on the
reader. The admin floor is deployment-wide and therefore applies under either subject —
it is the per-user preferences that differ, and those belong to whoever is reading.

**The policy is resolved at RUN time, inside the task — never serialized into the
signature.** An admin who tightens the floor between the click and the render should be
obeyed by the artifact the render produces; only a run-time read can do that. Passing an
``EffectiveRedactionConfig`` through the Celery signature would also pin the task to a
Python class shape across a rolling deploy (a message queued by the old code, unpacked by
a new worker), and would put ``custom_words``/``allowlist`` — which are by construction a
list of strings the user considers sensitive — into the broker in plaintext. A ``user_id``
is a stable primitive and re-resolving costs two indexed SELECTs.

The one thing that *is* captured at dispatch is :func:`export_policy_fingerprint`, and it
is used only for **routing** (the Redis dedup guard and the SSE channel variant), never
for a masking decision. See ``api/endpoints/files/__init__.py``.
"""

from __future__ import annotations

import hashlib
import json

from app.core import constants as C  # noqa: N812
from app.services.redaction.config import EffectiveRedactionConfig


class ExportRedactionNotReadyError(Exception):
    """The reader's policy masks this file, but its detection scan has no spans yet.

    Raised instead of rendering: the generators mask with **cached** spans, so an
    export produced now would be the raw transcript with nothing in the file to say
    the scan was incomplete. The single-file endpoint answers 409 for the same
    condition; a worker path publishes an error event and produces no artifact.
    """


def export_masking_is_pending(cfg: EffectiveRedactionConfig | None, status: str | None) -> bool:
    """Whether an export must be withheld until the file's detection scan finishes.

    The status rule ``api/endpoints/files/crud.py::_redaction_pending`` applies, minus
    its lazy re-dispatch: ``done`` and ``failed`` never withhold (``failed`` means the
    scan could not run, and trapping the user forever is worse than the read the page
    already allows), anything else does. A worker deliberately does **not** dispatch a
    scan of its own — the transcript page the file would be exported from does that, and
    a background writer here would be a DB write inside a download task for no gain.

    Args:
        cfg: The reader's effective policy. ``None`` or disabled → never withhold.
        status: ``media_file.redaction_status``.

    Returns:
        True when the export must not be produced yet.
    """
    if cfg is None or not cfg.enabled:
        return False
    return status not in (C.REDACTION_STATUS_DONE, C.REDACTION_STATUS_FAILED)


def export_policy_fingerprint(cfg: EffectiveRedactionConfig | None) -> str:
    """Short stable digest of everything about ``cfg`` that changes exported TEXT.

    A burned-in-subtitle video is cached in object storage and re-served on the next
    request, so the cache key has to name the policy the pixels were rendered under.
    Without this, the first reader to render a file freezes their masking for everyone:
    an admin switching ``force_export_redacted`` on would keep serving the unmasked
    video that was already cached, and a shared file rendered by an owner with redaction
    off would be handed to a reader whose policy masks.

    Returns ``""`` whenever the policy masks nothing, so a deployment with redaction
    disabled keeps byte-identical cache keys and its existing cached objects stay valid.

    ``toxicity_threshold`` is deliberately absent: it gates the UI's toxic-segment flag
    (``is_segment_toxic``) and never a mask — the ``toxicity`` category's spans come from
    the ``llm`` detector and are filtered by ``enabled_categories`` like any other.
    """
    if cfg is None or not cfg.enabled or not cfg.enabled_categories:
        return ""
    material = json.dumps(
        {
            "categories": sorted(cfg.enabled_categories),
            "style": cfg.style,
            "pii_entities": sorted(cfg.pii_entities),
            "custom_words": sorted({w.strip().lower() for w in cfg.custom_words if w.strip()}),
            "allowlist": sorted({w.strip().lower() for w in cfg.allowlist if w.strip()}),
        },
        sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]
