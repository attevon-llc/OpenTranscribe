"""The dev frontend image must drop root and repair reused volumes (issue #1372).

``docker-compose.override.yml`` bind-mounts ``./frontend`` and mounts anonymous volumes at
``/app/node_modules`` and ``/app/.svelte-kit``. A root-run dev server leaves root-owned files in the
checkout. A plain ``USER node`` is not enough: Docker reuses an existing volume as-is (it only
populates an empty one from the image), so stacks created by the old root image crash-looped with
EACCES. The image therefore starts an entrypoint that chowns those volumes and then drops privileges.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = REPO_ROOT / "frontend" / "Dockerfile.dev"
ENTRYPOINT = REPO_ROOT / "frontend" / "docker-entrypoint.dev.sh"
VOLUMES = ("/app/node_modules", "/app/.svelte-kit")


def entrypoint_script(dockerfile: str) -> str | None:
    """The path the Dockerfile's ENTRYPOINT exec-form points at, or None."""
    m = re.search(r'^\s*ENTRYPOINT\s+\["([^"]+)"', dockerfile, flags=re.MULTILINE)
    return m.group(1) if m else None


def entrypoint_problems(script: str) -> list[str]:
    """What is missing from an entrypoint that must repair volumes, then drop root."""
    problems = []
    for vol in VOLUMES:
        if vol not in script:
            problems.append(f"does not repair {vol}")
    if not re.search(r"chown -R", script):
        problems.append("never chowns")
    if not re.search(r'stat -c %u "\$d"\)" != ', script):
        problems.append("chowns unconditionally (must compare the top-level owner first)")
    if not re.search(r"^\s*exec su-exec ", script, flags=re.MULTILINE):
        problems.append("does not exec su-exec to drop privileges")
    if re.search(r"chown\b[^\n]*\s/app(\s|$|\")", script):
        problems.append("chowns the bind-mounted source")
    return problems


def test_dockerfile_wires_the_entrypoint_and_installs_su_exec():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert entrypoint_script(text) == "/usr/local/bin/docker-entrypoint.dev.sh"
    assert "docker-entrypoint.dev.sh" in text and re.search(r"\bsu-exec\b", text)
    # The entrypoint needs root to chown; a USER line would defeat it.
    assert not re.search(r"^\s*USER\s", text, flags=re.MULTILINE)


def test_entrypoint_repairs_volumes_then_drops_root():
    script = ENTRYPOINT.read_text(encoding="utf-8")
    assert entrypoint_problems(script) == []


def test_checks_fire_on_bad_entrypoints_and_stay_clean_on_the_real_shape():
    good = (
        "for d in /app/node_modules /app/.svelte-kit; do\n"
        '  if [ "$(stat -c %u "$d")" != "$uid" ]; then chown -R "$uid:$gid" "$d"; fi\n'
        'done\nexec su-exec "$uid:$gid" "$@"\n'
    )
    assert entrypoint_problems(good) == []
    assert "never chowns" in entrypoint_problems('exec su-exec 1000 "$@"')
    assert any("unconditionally" in p for p in entrypoint_problems(good.replace('!= "$uid"', "")))
    assert "does not exec su-exec to drop privileges" in entrypoint_problems(
        good.replace("exec su-exec", "exec")
    )
    assert "chowns the bind-mounted source" in entrypoint_problems(good + "chown -R 1000 /app\n")
    assert entrypoint_script('FROM x\nENTRYPOINT ["/a.sh"]') == "/a.sh"
    assert entrypoint_script("FROM x\n") is None
