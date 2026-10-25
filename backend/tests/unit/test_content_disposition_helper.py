"""``attachment_header`` builds the one RFC 6266 value every download route sends (#1283)."""

from __future__ import annotations

import re
from urllib.parse import unquote

import pytest

from app.utils.content_disposition import attachment_header

pytestmark = pytest.mark.unit

_EXT = re.compile(r"filename\*=UTF-8''(?P<v>\S+)$")
_PLAIN = re.compile(r'filename="(?P<v>[^"]*)"')


def _decoded(header: str) -> str:
    match = _EXT.search(header)
    assert match is not None, header
    return unquote(match.group("v"))


def test_an_ascii_name_is_carried_verbatim_in_both_parameters() -> None:
    assert attachment_header("a b.txt") == (
        "attachment; filename=\"a b.txt\"; filename*=UTF-8''a%20b.txt"
    )


@pytest.mark.parametrize(
    ("name", "fallback"),
    [
        ("اجتماع الميزانية.txt", "download.txt"),
        ("会议记录.md", "download.md"),
        ("Réunion café.srt", "Reunion cafe.srt"),
        ("اجتماع.", "download"),
    ],
)
def test_a_non_ascii_name_gets_an_ascii_fallback_and_an_exact_utf8_form(
    name: str, fallback: str
) -> None:
    header = attachment_header(name)
    header.encode("latin-1")  # what Starlette does to every header value
    plain = _PLAIN.search(header)
    assert plain is not None and plain.group("v") == fallback
    assert _decoded(header) == name


def test_control_characters_and_quotes_cannot_escape_the_header() -> None:
    header = attachment_header('evil";\r\nX-Injected: 1.txt')
    assert "\r" not in header and "\n" not in header
    assert header.count('"') == 2
    assert "\x00" not in attachment_header("a\x00b.txt")


def test_path_separators_are_neutralised() -> None:
    header = attachment_header("../etc\\passwd.txt")
    assert _decoded(header) == ".._etc_passwd.txt"
    plain = _PLAIN.search(header)
    assert plain is not None and plain.group("v") == ".._etc_passwd.txt"


def test_bidi_override_characters_are_stripped_from_the_utf8_form() -> None:
    assert _decoded(attachment_header("report‮gpj.exe")) == "reportgpj.exe"


@pytest.mark.parametrize("name", ["", "   ", "\r\n"])
def test_an_empty_name_still_yields_a_usable_filename(name: str) -> None:
    assert attachment_header(name).startswith('attachment; filename="download"')


def test_the_disposition_type_is_selectable() -> None:
    assert attachment_header("t.png", disposition="inline").startswith("inline; ")
