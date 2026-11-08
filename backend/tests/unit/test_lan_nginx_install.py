"""Small-business LAN install over nginx: the pieces that need no running stack (issue #1239).

A green ``curl`` against port 443 proves one of four independent moving parts. These tests pin
the ones that can be checked statically *and for real*: they run the compose ``command`` that
renders ``nginx/site.conf.template`` rather than grepping the template, run
``generate-ssl-cert.sh`` and read the SAN back with ``openssl``, and scan the docs for the
claim that used to be false (``start dev`` loads nginx - it does not).

Not covered here, and said so on the docs page: a browser on a second device, certificate trust
on iOS/Android, and a real microphone.
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_NGINX = REPO_ROOT / "docker-compose.nginx.yml"
TEMPLATE = REPO_ROOT / "nginx" / "site.conf.template"
CERT_SCRIPT = REPO_ROOT / "scripts" / "generate-ssl-cert.sh"

needs_envsubst = pytest.mark.skipif(
    shutil.which("envsubst") is None, reason="envsubst (gettext) not installed"
)
needs_openssl = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not installed")


def _interpolate(command: str, env: dict[str, str]) -> str:
    """Apply compose interpolation: ``$$`` -> ``$`` and ``${VAR:-default}``."""

    def _sub(match: re.Match[str]) -> str:
        if match.group(0) == "$$":
            return "$"
        return env.get(match.group(1), match.group(2) or "")

    return re.sub(r"\$\$|\$\{(\w+)(?::-([^}]*))?\}", _sub, command)


def _render_site_conf(tmp_path: Path, env: dict[str, str]) -> str:
    """Run the nginx service's own startup ``command`` and return the config it writes.

    Only the three container paths and the final ``nginx`` exec are swapped, each asserted to
    have been present, so a change to the command shape fails here instead of passing vacuously.
    """
    command = yaml.safe_load(COMPOSE_NGINX.read_text(encoding="utf-8"))["services"]["nginx"][
        "command"
    ]
    out = tmp_path / "site.conf"
    command = _interpolate(command, env)
    for needle, replacement in (
        ("/etc/nginx/templates/site.conf.template", str(TEMPLATE)),
        ("/etc/nginx/conf.d/site.conf", str(out)),
        ("nginx -g 'daemon off;'", "true"),
    ):
        assert needle in command, f"nginx command no longer contains {needle!r}"
        command = command.replace(needle, replacement)
    # Compose splits a string `command` into argv like this; running the whole string through
    # an outer shell instead would expand the inner script's `$VARS` before it ever ran.
    result = subprocess.run(
        shlex.split(command),
        env={"PATH": "/usr/bin:/bin", **env},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return out.read_text(encoding="utf-8")


def _http_redirect(rendered: str) -> str:
    match = re.search(r"return 301 (\S+);", rendered)
    assert match, "rendered config has no HTTP->HTTPS redirect"
    return match.group(1)


@needs_envsubst
@pytest.mark.parametrize(
    ("https_port", "expected"),
    [
        (None, "https://$host$request_uri"),
        ("443", "https://$host$request_uri"),
        ("8443", "https://$host:8443$request_uri"),
    ],
)
def test_http_redirect_keeps_a_non_default_https_port(tmp_path, https_port, expected):
    """``$host`` carries no port, so ``NGINX_HTTPS_PORT=8443`` sent every visitor to 443."""
    env = {"NGINX_SERVER_NAME": "192.0.2.10"}
    if https_port is not None:
        env["NGINX_HTTPS_PORT"] = https_port

    rendered = _render_site_conf(tmp_path, env)

    assert _http_redirect(rendered) == expected
    assert "${" not in rendered, "a template variable was not substituted"
    assert "server_name 192.0.2.10;" in rendered


@needs_envsubst
def test_every_template_variable_is_in_the_envsubst_whitelist():
    """A ``${VAR}`` the whitelist omits reaches nginx literally and fails its config parse."""
    command = yaml.safe_load(COMPOSE_NGINX.read_text(encoding="utf-8"))["services"]["nginx"][
        "command"
    ]
    assert "$$NGINX_HTTPS_REDIRECT_SUFFIX" in command
    template_vars = set(re.findall(r"\$\{(\w+)\}", TEMPLATE.read_text(encoding="utf-8")))
    whitelisted = set(re.findall(r"\$\$(\w+)", command.split("envsubst", 1)[1].split("<")[0]))
    assert template_vars <= whitelisted, (
        f"template uses {sorted(template_vars - whitelisted)} but envsubst would leave them literal"
    )


def _san(cert: Path) -> str:
    return subprocess.run(
        ["openssl", "x509", "-in", str(cert), "-noout", "-ext", "subjectAltName"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout


def _cert_script_copy(tmp_path: Path) -> Path:
    """The script resolves its output dir from its own location: run a copy, never the real one."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    copy = scripts / "generate-ssl-cert.sh"
    shutil.copy(CERT_SCRIPT, copy)
    return copy


@needs_openssl
def test_certificate_covers_the_hostname_and_the_given_ip(tmp_path):
    script = _cert_script_copy(tmp_path)

    result = subprocess.run(
        ["bash", str(script), "lan.example.test", "--ip", "192.0.2.10"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    san = _san(tmp_path / "nginx" / "ssl" / "server.crt")
    assert "DNS:lan.example.test" in san
    assert "IP Address:192.0.2.10" in san
    assert "IP Address:127.0.0.1" in san


@needs_openssl
@pytest.mark.parametrize(
    "args",
    [
        ["bad host"],
        ["a;b"],
        ["evil\nO = injected"],
        ["-leading-hyphen.test"],
        ["ok.test", "--ip", "999.1.1;x"],
        ["ok.test", "--ip", "not-an-ip"],
    ],
)
def test_certificate_script_rejects_input_before_writing_anything(tmp_path, args):
    """The hostname is spliced into an openssl config; refuse it before that happens."""
    script = _cert_script_copy(tmp_path)

    result = subprocess.run(
        ["bash", str(script), *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode != 0
    assert not (tmp_path / "nginx" / "ssl" / "server.crt").exists()
    assert not (tmp_path / "nginx" / "ssl" / "server.key").exists()


# ─── docs ────────────────────────────────────────────────────────────────────────────


def _dev_enables_nginx_claim(text: str) -> str | None:
    """First line claiming ``./opentr.sh start dev`` brings nginx up, else None.

    ``start dev`` skips the nginx overlay (``opentr.sh``: "skipped in dev mode"), so any
    run of lines that tells the reader to set NGINX_SERVER_NAME and then ``start dev`` is wrong.
    Keyed on ``NGINX_SERVER_NAME`` / "HTTPS enabled" so unrelated "auto-detects" prose (the
    Blackwell GPU overlay) is not a claim.
    A window that says "skip" is the correct explanation and is not a claim.
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if "opentr.sh start dev" not in line or re.search(r"skip", line, re.I):
            continue
        window = "\n".join(lines[max(0, i - 8) : i + 8])
        if re.search(r"skip", window, re.I):
            continue
        if re.search(r"NGINX_SERVER_NAME|HTTPS enabled", window):
            return line.strip()
    return None


def _doc_files() -> list[Path]:
    roots = [REPO_ROOT / "docs", REPO_ROOT / "docs-site" / "docs"]
    files = [REPO_ROOT / "README.md", COMPOSE_NGINX]
    for root in roots:
        files += [p for p in root.rglob("*") if p.suffix in {".md", ".mdx"}]
    return files


def test_the_dev_claim_detector_fires_on_the_old_text_and_not_the_new():
    old = (
        "### Step 4: Start OpenTranscribe\n\n```bash\n./opentr.sh start dev\n```\n\n"
        "The script automatically detects `NGINX_SERVER_NAME` and includes the NGINX overlay.\n"
    )
    new = "NGINX is loaded in production only: `./opentr.sh start dev` skips it.\n"
    assert _dev_enables_nginx_claim(old) == "./opentr.sh start dev"
    assert _dev_enables_nginx_claim(new) is None


def test_no_doc_says_start_dev_enables_nginx():
    offenders = {
        str(p.relative_to(REPO_ROOT)): claim
        for p in _doc_files()
        if (claim := _dev_enables_nginx_claim(p.read_text(encoding="utf-8")))
    }
    assert not offenders, f"these still say `start dev` enables nginx: {offenders}"


def test_the_old_nginx_guide_is_a_pointer_not_a_second_copy():
    """Two hand-maintained copies already disagreed; one home, the other links to it."""
    old = (REPO_ROOT / "docs" / "NGINX_SETUP.md").read_text(encoding="utf-8")
    page = REPO_ROOT / "docs-site" / "docs" / "installation" / "small-business-lan.md"
    assert page.is_file()
    assert len(old.splitlines()) < 40
    assert "installation/small-business-lan.md" in old
    assert "installation/small-business-lan" in (REPO_ROOT / "docs-site" / "sidebars.ts").read_text(
        encoding="utf-8"
    )


def test_opentr_help_does_not_send_https_users_to_dev():
    """The help text's HTTPS recipe is the one place ``opentr.sh`` itself tells people how."""
    text = (REPO_ROOT / "opentr.sh").read_text(encoding="utf-8")
    start = text.index("HTTPS/SSL Setup (for microphone recording")
    block = text[start : text.index('echo "Examples:"', start)]
    assert "NGINX_SERVER_NAME" in block, "the HTTPS help block moved; update this test"
    assert "start dev" not in block
    assert "start prod" in block


# ─── local CA + leaf (issue #1264) ───────────────────────────────────────────────────


def _x509_text(cert: Path) -> str:
    return subprocess.run(
        ["openssl", "x509", "-in", str(cert), "-noout", "-text"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout


def _generate(tmp_path: Path, *args: str) -> Path:
    script = (
        tmp_path / "scripts" / "generate-ssl-cert.sh"
        if (tmp_path / "scripts").exists()
        else _cert_script_copy(tmp_path)
    )
    result = subprocess.run(
        ["bash", str(script), "lan.example.test", *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return tmp_path / "nginx" / "ssl"


@needs_openssl
def test_server_certificate_is_issued_by_a_local_ca_devices_can_trust(tmp_path):
    ssl_dir = _generate(tmp_path, "--ip", "192.0.2.10")

    ca_text = _x509_text(ssl_dir / "ca.crt")
    assert re.search(r"Basic Constraints: critical\s+CA:TRUE", ca_text)
    assert re.search(r"Key Usage: critical\s+Certificate Sign, CRL Sign", ca_text)

    leaf_text = _x509_text(ssl_dir / "server.crt")
    assert re.search(r"Basic Constraints: critical\s+CA:FALSE", leaf_text)
    assert "TLS Web Server Authentication" in leaf_text
    assert "Issuer: O = OpenTranscribe, CN = OpenTranscribe Local CA" in leaf_text

    # The chain a device would build: trusting only ca.crt must validate the leaf.
    verify = subprocess.run(
        ["openssl", "verify", "-CAfile", str(ssl_dir / "ca.crt"), str(ssl_dir / "server.crt")],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert verify.returncode == 0, verify.stdout + verify.stderr


@needs_openssl
def test_server_certificate_validity_is_within_apples_825_day_limit(tmp_path):
    ssl_dir = _generate(tmp_path)
    out = subprocess.run(
        ["openssl", "x509", "-in", str(ssl_dir / "server.crt"), "-noout", "-startdate", "-enddate"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout
    fmt = "%b %d %H:%M:%S %Y %Z"
    start, end = (
        datetime.strptime(line.split("=", 1)[1].strip().replace("  ", " "), fmt)
        for line in out.strip().splitlines()
    )
    assert timedelta(days=0) < end - start <= timedelta(days=825)


@needs_openssl
def test_private_keys_are_owner_only(tmp_path):
    ssl_dir = _generate(tmp_path)
    for name in ("ca.key", "server.key"):
        assert oct((ssl_dir / name).stat().st_mode & 0o777) == "0o600", name


@needs_openssl
def test_rerunning_reuses_the_ca_and_reissues_only_the_leaf(tmp_path):
    ssl_dir = _generate(tmp_path)
    ca_before = (ssl_dir / "ca.crt").read_bytes()
    ca_key_before = (ssl_dir / "ca.key").read_bytes()
    leaf_before = (ssl_dir / "server.crt").read_bytes()

    _generate(tmp_path, "--ip", "192.0.2.77")

    assert (ssl_dir / "ca.crt").read_bytes() == ca_before
    assert (ssl_dir / "ca.key").read_bytes() == ca_key_before
    assert (ssl_dir / "server.crt").read_bytes() != leaf_before
    assert "IP Address:192.0.2.77" in _san(ssl_dir / "server.crt")
    verify = subprocess.run(
        ["openssl", "verify", "-CAfile", str(ssl_dir / "ca.crt"), str(ssl_dir / "server.crt")],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert verify.returncode == 0, verify.stdout + verify.stderr


@needs_openssl
def test_a_ca_whose_key_does_not_match_is_replaced_not_trusted(tmp_path):
    ssl_dir = _generate(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    other_dir = _generate(other)
    (ssl_dir / "ca.key").write_bytes((other_dir / "ca.key").read_bytes())
    ca_before = (ssl_dir / "ca.crt").read_bytes()

    _generate(tmp_path)

    assert (ssl_dir / "ca.crt").read_bytes() != ca_before


# ─── wording (issue #1264) ───────────────────────────────────────────────────────────

SETUP_SCRIPT = REPO_ROOT / "setup-opentranscribe.sh"
MANAGER_SCRIPT = REPO_ROOT / "opentranscribe.sh"


def test_the_installer_tells_people_to_trust_the_ca_not_the_server_certificate():
    for path in (SETUP_SCRIPT, MANAGER_SCRIPT):
        text = path.read_text(encoding="utf-8")
        assert "ca.crt" in text, path.name
        assert "Trust nginx/ssl/server.crt" not in text, path.name
        assert "Copy nginx/ssl/server.crt" not in text, path.name
        assert "trust the self-signed certificate" not in text, path.name


def test_the_plain_http_warning_says_what_is_exposed_in_plain_words():
    text = SETUP_SCRIPT.read_text(encoding="utf-8")
    start = text.index("If you skip HTTPS:")
    block = text[start : text.index("# On re-run where user previously said No", start)]
    for needed in ("anyone on it can read them", "password", "microphone"):
        assert needed in block, f"warning no longer mentions {needed!r}"


def test_the_docs_describe_the_ca_flow():
    page = (REPO_ROOT / "docs-site/docs/installation/small-business-lan.md").read_text(
        encoding="utf-8"
    )
    assert "ca.crt" in page
    assert "825" in page
    assert "nginx/ssl/ca.key" in page
    assert "Copy\n`nginx/ssl/server.crt`" not in page
