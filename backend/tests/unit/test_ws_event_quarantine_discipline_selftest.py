"""Guard the guard: every detector in test_ws_event_quarantine_discipline.py
needs a must-fire and a must-stay-clean case.

A detector that matches nothing reports zero findings, which is indistinguishable
from a clean tree — the exact failure mode issue #431 named in two other auditors
in this repo (each had detectors that silently matched nothing). Written as
source strings parsed with ``ast``, mirroring
``test_ddl_marker_discipline_selftest.py``.
"""

from __future__ import annotations

import ast
import textwrap

from tests.unit.test_ws_event_quarantine_discipline import _RAW_CHANNEL
from tests.unit.test_ws_event_quarantine_discipline import _EnclosingFunctionWalker
from tests.unit.test_ws_event_quarantine_discipline import _send_ws_event_aliases


def _walk(source: str) -> _EnclosingFunctionWalker:
    tree = ast.parse(textwrap.dedent(source))
    walker = _EnclosingFunctionWalker()
    walker.configure(_send_ws_event_aliases(tree))
    walker.visit(tree)
    return walker


# --- Detector 1: plain `send_ws_event(...)` (ast.Name call) ---

_PLAIN_NAME_CALL = """
    def notify(user_id, data):
        send_ws_event(user_id, "file_updated", data)
"""

_PLAIN_NAME_CLEAN = """
    def notify(user_id, data):
        send_ws_event_for_file(user_id, "file_updated", data, file_id=1)
"""


def test_plain_name_call_must_fire() -> None:
    findings = _walk(_PLAIN_NAME_CALL).direct_send_findings
    assert findings == [("notify", 3)]


def test_plain_name_call_must_stay_clean_when_guarded() -> None:
    assert _walk(_PLAIN_NAME_CLEAN).direct_send_findings == []


# --- Detector 2: `module.send_ws_event(...)` (ast.Attribute call) ---

_ATTRIBUTE_CALL = """
    from app.utils import websocket_notify

    def notify(user_id, data):
        websocket_notify.send_ws_event(user_id, "file_updated", data)
"""

_ATTRIBUTE_CLEAN = """
    from app.utils import websocket_notify

    def notify(user_id, data):
        websocket_notify.send_ws_event_for_file(user_id, "file_updated", data, file_id=1)
"""


def test_attribute_call_must_fire() -> None:
    findings = _walk(_ATTRIBUTE_CALL).direct_send_findings
    assert findings == [("notify", 5)]


def test_attribute_call_must_stay_clean_when_guarded() -> None:
    assert _walk(_ATTRIBUTE_CLEAN).direct_send_findings == []


# --- Detector 3: aliased import (`from ... import send_ws_event as _push`) ---

_ALIASED_IMPORT_CALL = """
    from app.utils.websocket_notify import send_ws_event as _push

    def notify(user_id, data):
        _push(user_id, "file_updated", data)
"""

_ALIASED_IMPORT_CLEAN = """
    from app.utils.websocket_notify import send_ws_event_for_file as _push

    def notify(user_id, data):
        _push(user_id, "file_updated", data, file_id=1)
"""


def test_aliased_import_call_must_fire() -> None:
    findings = _walk(_ALIASED_IMPORT_CALL).direct_send_findings
    assert findings == [("notify", 5)]


def test_aliased_import_call_must_stay_clean_when_guarded() -> None:
    # A call through an alias of send_ws_event_for_file is not a `send_ws_event`
    # alias at all — _send_ws_event_aliases only tracks aliases of the raw
    # primitive, so this must not be misclassified as a direct-send finding.
    assert _walk(_ALIASED_IMPORT_CLEAN).direct_send_findings == []


# --- Detector 4: nested functions attribute correctly to the INNERMOST enclosing fn ---

_NESTED_FUNCTION = """
    def outer(user_id, data):
        def inner():
            send_ws_event(user_id, "file_updated", data)
        inner()
"""


def test_nested_function_attributes_to_the_innermost_function() -> None:
    findings = _walk(_NESTED_FUNCTION).direct_send_findings
    assert findings == [("inner", 4)]


# --- Detector 5: module-level call (no enclosing function) ---

_MODULE_LEVEL_CALL = """
    send_ws_event(1, "file_updated", {})
"""


def test_module_level_call_attributes_to_module() -> None:
    findings = _walk(_MODULE_LEVEL_CALL).direct_send_findings
    assert findings == [("<module>", 2)]


# --- Detector 6: raw `websocket_notifications` channel publish ---

_RAW_PUBLISH_CALL = """
    def broadcast(client, payload):
        client.publish("websocket_notifications", payload)
"""

_RAW_PUBLISH_OTHER_CHANNEL = """
    def broadcast(client, payload):
        client.publish("some_other_channel", payload)
"""


def test_raw_publish_to_the_named_channel_must_fire() -> None:
    findings = _walk(_RAW_PUBLISH_CALL).raw_publish_findings
    assert findings == [("broadcast", 3)]


def test_raw_publish_to_a_different_channel_must_stay_clean() -> None:
    assert _walk(_RAW_PUBLISH_OTHER_CHANNEL).raw_publish_findings == []


def test_raw_channel_constant_is_the_real_one() -> None:
    """Pins the literal the scanner matches against, so a typo in either the
    scanner or this file cannot silently agree with itself."""
    assert _RAW_CHANNEL == "websocket_notifications"


# --- Detector 7: send_ws_event_for_file calls with/without a selector keyword ---

_FOR_FILE_NO_SELECTOR = """
    def notify(user_id, data):
        send_ws_event_for_file(user_id, "file_updated", data)
"""

_FOR_FILE_WITH_SELECTOR = """
    def notify(user_id, data):
        send_ws_event_for_file(user_id, "file_updated", data, file_uuid="x")
"""


def test_for_file_call_with_no_selector_must_fire() -> None:
    findings = _walk(_FOR_FILE_NO_SELECTOR).send_ws_event_for_file_findings
    assert len(findings) == 1
    fn, lineno, call = findings[0]
    assert fn == "notify"
    given = {kw.arg for kw in call.keywords if kw.arg in {"file_id", "file_uuid", "file_uuids"}}
    assert given == set()


def test_for_file_call_with_a_selector_must_stay_clean() -> None:
    findings = _walk(_FOR_FILE_WITH_SELECTOR).send_ws_event_for_file_findings
    assert len(findings) == 1
    _, _, call = findings[0]
    given = {kw.arg for kw in call.keywords if kw.arg in {"file_id", "file_uuid", "file_uuids"}}
    assert given == {"file_uuid"}
