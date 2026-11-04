"""scripts/check-docs-live.py against a local fake docs site.

Each test builds a temp repo tree (VERSION, blog, upgrade page, sidebars) and a tiny
``http.server`` site, then runs the real script as a subprocess so exit codes are what the
workflow sees. The expectations are derived from the temp tree, never from this repo's VERSION.
"""

from __future__ import annotations

import http.server
import subprocess
import sys
import threading
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from email.utils import format_datetime
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "check-docs-live.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "docs-live-check.yml"

pytestmark = pytest.mark.skipif(not SCRIPT.is_file(), reason="check-docs-live.py not present")

KEY_IDS = [
    "getting-started/quick-start",
    "installation/docker-compose",
    "configuration/environment-variables",
]


def _make_repo(root: Path, version: str = "1.4.2") -> None:
    (root / "VERSION").write_text(f"v{version}\n")
    blog = root / "docs-site" / "blog"
    blog.mkdir(parents=True)
    (blog / "2025-01-01-old-post.md").write_text("---\ntitle: old\n---\n")
    (blog / "2026-02-03-v1.4.2-release.md").write_text("---\nslug: v1.4.2-release\n---\n")
    (blog / "authors.yml").write_text("x: {}\n")
    docs = root / "docs-site" / "docs"
    (docs / "operations").mkdir(parents=True)
    (docs / "operations" / "upgrading-to-1-3-0.md").write_text("x")
    (docs / "operations" / "upgrading-to-1-4-2.md").write_text("x")
    for kid in KEY_IDS:
        (docs / kid).parent.mkdir(parents=True, exist_ok=True)
        (docs / f"{kid}.md").write_text("x")
    items = ",".join(f"'{k}'" for k in KEY_IDS)
    (root / "docs-site" / "sidebars.ts").write_text(
        f"export default {{ s: [{items}] }};\n// 'api/commented-out',\n"
    )


def _site(version="1.4.2", missing=(), last_modified=None, listed=True, in_sitemap=True):
    home = f'<div class="heroBadges_ab"><span class="badge">v{version}</span></div>'
    blog = "/blog/v1.4.2-release" if listed else "/blog/other"
    loc = "https://x/blog/v1.4.2-release" if in_sitemap else "https://x/blog/other"
    pages = {
        "/": (200, home),
        "/blog/": (200, f'<a href="{blog}">post</a>'),
        "/blog/v1.4.2-release": (200, "post"),
        "/docs/operations/upgrading-to-1-4-2": (200, "up"),
        "/sitemap.xml": (200, f"<urlset><url><loc>{loc}</loc></url></urlset>"),
    }
    for kid in KEY_IDS:
        pages[f"/docs/{kid}"] = (200, "page")
    for m in missing:
        pages.pop(m, None)
    return pages, last_modified


@pytest.fixture
def serve():
    servers = []

    def start(pages, last_modified=None) -> str:
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - http.server API
                status, body = pages.get(self.path, (404, "nope"))
                self.send_response(status)
                if last_modified:
                    self.send_header("Last-Modified", format_datetime(last_modified, usegmt=True))
                self.end_headers()
                self.wfile.write(body.encode())

            def log_message(self, *a):
                pass

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}"

    yield start
    for s in servers:
        s.shutdown()
        s.server_close()


def _run(base, repo, *extra):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--base-url", base, "--repo-root", str(repo), *extra],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.fixture
def repo(tmp_path):
    _make_repo(tmp_path)
    return tmp_path


def test_all_checks_pass(serve, repo):
    pages, _ = _site()
    r = _run(serve(pages), repo)
    assert r.returncode == 0, r.stdout
    for check in ("a home", "b version", "c newest blog", "d upgrade", "e key pages", "f sitemap"):
        assert f"PASS    {check}" in r.stdout
    # derived, not hard-coded: newest of the two upgrade pages and the dated blog file
    assert "/blog/v1.4.2-release" in r.stdout
    assert "upgrading-to-1-4-2" in r.stdout


def test_stale_version_fails(serve, repo):
    pages, _ = _site(version="1.3.0")
    r = _run(serve(pages), repo)
    assert r.returncode == 1
    assert "FAIL    b version: site shows 1.3.0, expected 1.4.2" in r.stdout


def test_missing_blog_post_fails(serve, repo):
    pages, _ = _site(missing=["/blog/v1.4.2-release"], listed=False, in_sitemap=False)
    r = _run(serve(pages), repo)
    assert r.returncode == 1
    assert "FAIL    c newest blog" in r.stdout
    assert "FAIL    f sitemap" in r.stdout


def test_404_key_page_fails(serve, repo):
    pages, _ = _site(missing=["/docs/installation/docker-compose"])
    r = _run(serve(pages), repo)
    assert r.returncode == 1
    assert "FAIL    e key pages: installation/docker-compose -> 404" in r.stdout
    # the commented-out sidebar entry is not probed
    assert "commented-out" not in r.stdout


def test_unreachable_host_exits_3(repo):
    r = _run("http://127.0.0.1:9", repo)
    assert r.returncode == 3
    assert "UNREACHABLE" in r.stdout


def test_deploy_in_progress_within_grace_is_not_a_failure(serve, repo):
    now = datetime.now(UTC)
    pages, _ = _site(version="1.3.0")
    base = serve(pages, last_modified=now - timedelta(days=1))
    r = _run(base, repo, "--last-change-time", (now - timedelta(minutes=10)).isoformat())
    assert r.returncode == 0, r.stdout
    assert "PENDING g freshness: deploy queued/in progress" in r.stdout
    assert "FAIL" not in r.stdout


def test_stale_beyond_grace_fails_as_stale(serve, repo):
    now = datetime.now(UTC)
    pages, _ = _site()
    base = serve(pages, last_modified=now - timedelta(days=1))
    r = _run(base, repo, "--last-change-time", (now - timedelta(minutes=200)).isoformat())
    assert r.returncode == 1
    assert "FAIL    g freshness: STALE" in r.stdout


def test_fresh_site_passes_freshness(serve, repo):
    now = datetime.now(UTC)
    pages, _ = _site()
    base = serve(pages, last_modified=now)
    r = _run(base, repo, "--last-change-time", (now - timedelta(hours=5)).isoformat())
    assert r.returncode == 0, r.stdout
    assert "PASS    g freshness" in r.stdout


def test_expected_version_flag_overrides_version_file(serve, repo):
    pages, _ = _site(version="9.9.9")
    r = _run(serve(pages), repo, "--expected-version", "v9.9.9")
    assert "PASS    b version" in r.stdout


def test_workflow_triggers_and_least_privilege():
    wf = yaml.safe_load(WORKFLOW.read_text())
    on = wf.get("on", wf.get(True))
    assert on["workflow_run"]["workflows"] == ["Deploy Documentation"]
    assert on["workflow_run"]["types"] == ["completed"]
    assert "schedule" in on
    assert "workflow_dispatch" in on
    assert wf["permissions"] == {"contents": "read"}
    writers = {
        name: job["permissions"]
        for name, job in wf["jobs"].items()
        if "write" in str(job.get("permissions", ""))
    }
    assert list(writers) == ["alert"]
    assert writers["alert"] == {"contents": "read", "issues": "write"}
