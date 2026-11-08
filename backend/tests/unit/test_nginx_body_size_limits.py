"""The bundled nginx configs accept a large request body only where a file is uploaded.

A server-wide (or ``/api/``-wide) ``client_max_body_size 15G`` let an anonymous caller
stream gigabytes at any JSON route. The ceiling is now small everywhere, and 15G is
granted only to the upload locations: ``= /api/files`` (the API-mediated upload) and the
object-storage paths (``/s3/``, ``/minio/``) that presigned uploads use.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

CONFIGS = [
    REPO_ROOT / "nginx" / "site.conf.template",
    REPO_ROOT / "frontend" / "nginx.conf",
    REPO_ROOT / "frontend" / "nginx-pki.conf",
]

SMALL_LIMIT_BYTES = 10 * 1024 * 1024
UPLOAD_LOCATIONS = {"= /api/files", "/s3/", "^~ /s3/", "/minio/", "^~ /minio/"}

_UNITS = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}


def _bytes(value: str) -> int:
    match = re.fullmatch(r"(\d+)([kKmMgG]?)", value)
    assert match, f"unparseable client_max_body_size {value!r}"
    return int(match.group(1)) * _UNITS[match.group(2).lower()]


def _body_size_directives(text: str) -> list[tuple[int, str | None, int]]:
    """``(server_index, enclosing location or None, bytes)`` for every directive."""
    found: list[tuple[int, str | None, int]] = []
    depth = 0
    server = -1
    location: str | None = None
    location_depth = -1
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if re.match(r"server\s*\{", line):
            server += 1
        header = re.match(r"location\s+(.+?)\s*\{", line)
        if header and location is None:
            location = header.group(1)
            location_depth = depth
        directive = re.match(r"client_max_body_size\s+(\S+);", line)
        if directive:
            found.append((server, location, _bytes(directive.group(1))))
        depth += line.count("{") - line.count("}")
        if location is not None and depth <= location_depth:
            location = None
    return found


@pytest.mark.parametrize("config", CONFIGS, ids=lambda p: p.name)
def test_large_bodies_are_granted_only_to_upload_locations(config: Path):
    directives = _body_size_directives(config.read_text(encoding="utf-8"))

    too_large = [(loc, size) for _srv, loc, size in directives if size > SMALL_LIMIT_BYTES]
    assert too_large, "expected the upload locations to keep a large ceiling"
    for location, size in too_large:
        assert location in UPLOAD_LOCATIONS, (
            f"{config.name}: client_max_body_size {size} bytes on {location or 'the server'}; "
            "only upload locations may accept a large body"
        )


@pytest.mark.parametrize("config", CONFIGS, ids=lambda p: p.name)
def test_every_server_sets_a_small_default(config: Path):
    directives = _body_size_directives(config.read_text(encoding="utf-8"))

    servers_with_api = {srv for srv, loc, _ in directives if loc == "= /api/files"}
    server_level = {srv: size for srv, loc, size in directives if loc is None}
    assert servers_with_api, f"{config.name}: no `location = /api/files` upload location"
    for srv in servers_with_api:
        assert srv in server_level, f"{config.name}: server #{srv} sets no server-level ceiling"
        assert server_level[srv] <= SMALL_LIMIT_BYTES


@pytest.mark.parametrize("config", CONFIGS, ids=lambda p: p.name)
def test_the_upload_location_keeps_the_upload_ceiling_and_streams(config: Path):
    text = config.read_text(encoding="utf-8")
    blocks = re.findall(r"location = /api/files \{(.*?)\n    \}", text, flags=re.DOTALL)

    assert blocks, f"{config.name}: no `location = /api/files` block"
    for block in blocks:
        assert "client_max_body_size 15G;" in block
        assert "proxy_request_buffering off;" in block


def test_the_parser_sees_a_server_wide_large_ceiling():
    """Self-test: the layout this file forbids is what HEAD used to ship."""
    text = "server {\n    client_max_body_size 15G;\n    location /api/ {\n    }\n}\n"

    assert _body_size_directives(text) == [(0, None, 15 * 1024**3)]
