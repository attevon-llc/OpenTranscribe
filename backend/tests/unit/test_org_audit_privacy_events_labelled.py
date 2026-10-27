"""The privacy audit events an organization writes have labels in its audit screen (#1280).

``org.policy.change`` and the two purge events are stamped with the organization, so they
appear in its admin's audit log and in the event filter. The screen labels events from
``ORG_AUDIT_EVENT_KEYS`` in ``frontend/src/lib/i18n/keyMaps.ts`` (an event with no entry
renders as its raw dotted identifier), and every label must exist in every locale.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.auth.audit import AuditEventType

FRONTEND = Path(__file__).resolve().parents[3] / "frontend" / "src" / "lib" / "i18n"
EVENTS = {
    AuditEventType.ORG_POLICY_CHANGE: "orgAudit.event.policyChange",
    AuditEventType.PRIVACY_VOICEPRINT_PURGE: "orgAudit.event.voiceprintsPurged",
    AuditEventType.PRIVACY_GENDER_PURGE: "orgAudit.event.genderPurged",
}
LOCALES = sorted(p.stem for p in (FRONTEND / "locales").glob("*.json"))


def _org_audit_map() -> dict[str, str]:
    text = (FRONTEND / "keyMaps.ts").read_text(encoding="utf-8")
    block = text.split("export const ORG_AUDIT_EVENT_KEYS", 1)[1].split("};", 1)[0]
    return dict(re.findall(r"'([^']+)':\s*'([^']+)'", block))


def test_the_twelve_locales_are_all_present():
    assert len(LOCALES) == 12


@pytest.mark.parametrize("event", sorted(EVENTS, key=str))
def test_the_event_has_a_label_key_in_the_org_audit_map(event):
    assert _org_audit_map().get(event.value) == EVENTS[event]


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("key", sorted(EVENTS.values()))
def test_the_label_exists_in_every_locale_and_is_translated(locale, key):
    labels = json.loads((FRONTEND / "locales" / f"{locale}.json").read_text(encoding="utf-8"))
    english = json.loads((FRONTEND / "locales" / "en.json").read_text(encoding="utf-8"))

    assert labels[key].strip()
    if locale != "en":
        assert labels[key] != english[key], f"{locale} {key} is an untranslated English copy"
