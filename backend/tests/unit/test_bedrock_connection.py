"""The Bedrock connection resolver: precedence, packing, scrubbing, and single ownership.

``services/bedrock_settings.py`` is the only place that decides a Bedrock region and the only
place boto3 client keyword arguments (and so credentials) are built. The structural tests keep
it that way: a second ``boto3.client("bedrock...")`` call or a direct ``settings.BEDROCK_*``
read elsewhere would let a configuration's credentials be bypassed.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.core.config import settings
from app.services import bedrock_settings as bs
from app.services.bedrock_settings import BedrockConnection

_APP = Path(__file__).resolve().parents[2] / "app"

KEY_ID = "AKIAEXAMPLE0FAKE0KEY"  # gitleaks:allow - fabricated fixture
SECRET = "fakeSecretAccessKey0123456789ABCDEFGHIJ"  # gitleaks:allow - fabricated fixture


def test_region_prefers_the_configuration_over_the_server(monkeypatch):
    monkeypatch.setattr(settings, "BEDROCK_REGION", "eu-west-1")

    assert bs.resolve_region(BedrockConnection(region="us-west-2")) == "us-west-2"
    assert bs.resolve_region(BedrockConnection()) == "eu-west-1"
    assert bs.resolve_region(None) == "eu-west-1"


def test_region_is_empty_when_nothing_is_set(monkeypatch):
    monkeypatch.setattr(settings, "BEDROCK_REGION", "")

    assert bs.resolve_region(None) == ""


def test_default_model_uses_the_server_setting_then_the_coded_default(monkeypatch):
    from app.core.constants import DEFAULT_BEDROCK_MODEL_ID

    monkeypatch.setattr(settings, "BEDROCK_MODEL_NAME", "global.anthropic.claude-sonnet-4-6")
    assert bs.resolve_default_model() == "global.anthropic.claude-sonnet-4-6"

    monkeypatch.setattr(settings, "BEDROCK_MODEL_NAME", "")
    assert bs.resolve_default_model() == DEFAULT_BEDROCK_MODEL_ID


def test_client_kwargs_passes_the_pair_explicitly_only_when_the_configuration_has_one():
    with_creds = BedrockConnection("us-east-1", KEY_ID, SECRET, "tok")
    without = BedrockConnection(region="us-east-1")

    kw = bs.client_kwargs("us-east-1", with_creds)
    assert (kw["aws_access_key_id"], kw["aws_secret_access_key"]) == (KEY_ID, SECRET)
    assert kw["aws_session_token"] == "tok"

    for connection in (without, None):
        kw = bs.client_kwargs("us-east-1", connection)
        assert kw == {"region_name": "us-east-1"}


def test_a_half_pair_is_not_handed_to_boto3():
    half = BedrockConnection(region="us-east-1", access_key_id=KEY_ID)

    assert half.has_credentials is False
    assert "aws_access_key_id" not in bs.client_kwargs("us-east-1", half)


def test_repr_never_prints_a_secret():
    connection = BedrockConnection("us-east-1", KEY_ID, SECRET, "tok")

    assert KEY_ID not in repr(connection)
    assert SECRET not in repr(connection)
    assert "tok" not in repr(connection)


def test_pack_and_unpack_round_trip():
    connection = BedrockConnection("us-east-1", KEY_ID, SECRET, "tok")

    assert bs.unpack_stored(bs.pack_stored(connection)) == connection


def test_nothing_to_store_packs_to_none_and_garbage_unpacks_to_empty():
    assert bs.pack_stored(BedrockConnection()) is None
    assert bs.unpack_stored("not json") == BedrockConnection()
    assert bs.unpack_stored('["a"]') == BedrockConnection()
    assert bs.unpack_stored(None) == BedrockConnection()


def test_merge_keeps_stored_values_for_blank_fields():
    stored = BedrockConnection("us-east-1", KEY_ID, SECRET, "tok")

    merged = bs.merge_stored(
        stored,
        region=None,
        region_given=False,
        access_key_id=None,
        secret_access_key=None,
        session_token=None,
        clear_credentials=False,
    )

    assert merged == stored


def test_merge_replaces_the_pair_and_drops_the_old_token():
    stored = BedrockConnection("us-east-1", KEY_ID, SECRET, "tok")

    merged = bs.merge_stored(
        stored,
        region="eu-west-1",
        region_given=True,
        access_key_id="AKIANEWEXAMPLE0KEY0",  # gitleaks:allow - fabricated fixture
        secret_access_key="newFakeSecret0123456789ABCDEFG",  # gitleaks:allow - fabricated
        session_token=None,
        clear_credentials=False,
    )

    assert merged.region == "eu-west-1"
    assert merged.access_key_id == "AKIANEWEXAMPLE0KEY0"  # gitleaks:allow
    assert merged.session_token is None


def test_merge_clear_drops_only_the_credentials():
    stored = BedrockConnection("us-east-1", KEY_ID, SECRET, "tok")

    merged = bs.merge_stored(
        stored,
        region=None,
        region_given=False,
        access_key_id=None,
        secret_access_key=None,
        session_token=None,
        clear_credentials=True,
    )

    assert merged == BedrockConnection(region="us-east-1")


def test_scrub_removes_every_secret_from_text():
    connection = BedrockConnection("us-east-1", KEY_ID, SECRET, "tok-value-123")
    text = f"failed for {KEY_ID} with {SECRET} and tok-value-123"

    scrubbed = bs.scrub(text, connection)

    for secret in (KEY_ID, SECRET, "tok-value-123"):
        assert secret not in scrubbed
    assert bs.scrub(text, None) == text


def test_stream_errors_do_not_echo_the_configurations_secrets():
    from unittest.mock import MagicMock
    from unittest.mock import patch

    from app.services.llm_bedrock import stream_converse

    connection = BedrockConnection("us-east-1", KEY_ID, SECRET)
    client = MagicMock()
    client.converse_stream.side_effect = RuntimeError(f"boom {SECRET} {KEY_ID}")

    with patch("app.services.llm_bedrock._client", return_value=client):
        events = list(
            stream_converse(
                model="global.anthropic.claude-haiku-4-5-20251001-v1:0",
                region="us-east-1",
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=5,
                connection=connection,
            )
        )

    assert events[-1].type == "error"
    assert SECRET not in (events[-1].message or "")
    assert KEY_ID not in (events[-1].message or "")


# --------------------------------------------------------------------------
# Structure: one resolver, no bypass
# --------------------------------------------------------------------------


def _python_files():
    return [p for p in _APP.rglob("*.py") if "__pycache__" not in p.parts]


def test_only_the_resolver_hands_credentials_to_a_bedrock_client():
    """Among Bedrock-aware modules, only the resolver spells a boto3 credential keyword."""
    exempt = {
        "services/bedrock_settings.py",
        # the wire contract and the API layer that carries the user's entered values
        "schemas/llm_settings.py",
        "api/endpoints/llm_settings.py",
    }
    offenders = []
    for path in _python_files():
        rel = str(path.relative_to(_APP))
        text = path.read_text(encoding="utf-8")
        if rel in exempt or "bedrock" not in text.lower():
            continue
        if re.search(r"aws_(access_key_id|secret_access_key|session_token)\s*=\s*", text):
            offenders.append(rel)
    assert offenders == []


def test_bedrock_clients_are_built_in_one_function():
    """``llm_bedrock._client`` is the only constructor of a Bedrock boto3 client."""
    bedrock_literals = []
    constructors = []
    for path in _python_files():
        rel = str(path.relative_to(_APP))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "client"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "boto3"
            ):
                continue
            first = node.args[0] if node.args else None
            if isinstance(first, ast.Constant) and "bedrock" in str(first.value):
                bedrock_literals.append(rel)
            if rel == "services/llm_bedrock.py":
                constructors.append(node.lineno)
    assert bedrock_literals == []
    assert len(constructors) == 1


def test_the_runtime_never_reads_the_bedrock_settings_directly():
    """Region and model come from the resolver, so a configuration's own values win."""
    allowed = {"services/bedrock_settings.py", "core/config.py"}
    offenders = []
    for path in _python_files():
        rel = str(path.relative_to(_APP))
        if rel in allowed:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "settings"
                and node.attr in {"BEDROCK_REGION", "BEDROCK_MODEL_NAME"}
            ):
                offenders.append(f"{rel}:{node.lineno}")
    assert offenders == []


@pytest.mark.parametrize("name", ["stream_converse", "converse", "check_connection"])
def test_every_bedrock_call_takes_a_connection(name):
    import inspect

    from app.services import llm_bedrock

    assert "connection" in inspect.signature(getattr(llm_bedrock, name)).parameters
