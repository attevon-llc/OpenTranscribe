"""Build-efficiency guards for the frontend and docs images (v0.7.0 WASTE_AUDIT W3/W6/W10/W11/W12).

Each rule encodes a cost that was measured: a recursive ``chown`` in a build layer duplicates every
file it touches, an uncached ``npm ci`` re-downloads on every lockfile change, and root-owned
``node_modules`` forces the dev entrypoint to ``chown -R`` the whole tree on every fresh volume.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
DEV = REPO / "frontend" / "Dockerfile.dev"
PROD = REPO / "frontend" / "Dockerfile.prod"
DOCS = REPO / "docs-site" / "Dockerfile"
FONTS = REPO / "frontend" / "scripts" / "download-fonts.js"


def recursive_html_chown(text: str) -> bool:
    return bool(re.search(r"chown\s+-R\s+\S+\s+/usr/share/nginx/html", text))


def npm_install_lines(text: str) -> list[str]:
    joined = text.replace("\\\n", " ")
    return [
        ln for ln in joined.splitlines() if re.search(r"\bnpm (ci|install)\b", ln) and "RUN" in ln
    ]


def uncached(lines: list[str]) -> list[str]:
    return [ln for ln in lines if "--mount=type=cache" not in ln]


def test_nginx_html_is_chowned_by_copy_not_a_second_layer():
    for path in (PROD, DOCS):
        text = path.read_text(encoding="utf-8")
        assert not recursive_html_chown(text), path
        assert re.search(r"COPY --from=build --chown=nginx:nginx \S+ /usr/share/nginx/html", text)


def test_every_npm_install_uses_a_cache_mount():
    for path in (DEV, PROD, DOCS):
        lines = npm_install_lines(path.read_text(encoding="utf-8"))
        assert lines, path
        assert uncached(lines) == [], path


def test_dev_uses_npm_ci_as_node_with_matching_cache_owner():
    text = DEV.read_text(encoding="utf-8")
    assert not re.search(r"\bnpm install\b", text)
    assert "COPY --chown=node:node package.json package-lock.json" in text
    assert "su-exec node:node npm ci" in text
    assert not re.search(r"^\s*USER\s", text, flags=re.MULTILINE)
    assert "target=/home/node/.npm,uid=1000,gid=1000" in text


def test_no_unused_native_build_tools_or_dead_chmods():
    for path in (DEV, PROD):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"apk add[^\n]*(?:\\\n[^\n]*)*\b(g\+\+|python3|make)\b", text), path
        assert not re.search(r"RUN chmod -R 755 static/", text), path
    assert re.search(r"apk add --no-cache su-exec", DEV.read_text(encoding="utf-8"))


def test_download_fonts_skips_files_already_present():
    text = FONTS.read_text(encoding="utf-8")
    assert "function isValid" in text and "statSync(path).size > 0" in text
    assert re.search(r"if \(isValid\(localPath\)\)", text)
