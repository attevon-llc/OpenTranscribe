"""Docs that say "Settings → X" must name a real Settings sidebar entry (issue #1209).

The sidebar was regrouped repeatedly (Speech Processing became a tab, Auto-Label was
renamed and moved, "Transcription & AI" never existed) and the docs kept pointing at
labels a reader cannot find. The sidebar's English labels are the source of truth: every
``label: $t('…')`` and group ``title: $t('settings.sections.…')`` in ``SettingsModal.svelte``,
resolved through ``en.json``. Only the FIRST segment after ``Settings →`` is checked — tab
names below it are checked by the frontend tests that render the tabs.

A path that is about a device or browser Settings app, not this one, is exempted by file
with the reason written next to it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
MODAL = REPO_ROOT / "frontend" / "src" / "components" / "SettingsModal.svelte"
EN_JSON = REPO_ROOT / "frontend" / "src" / "lib" / "i18n" / "locales" / "en.json"
DOCS = REPO_ROOT / "docs-site" / "docs"

#: Section ids that no longer have a sidebar row but still open a tab; docs may use the
#: label the user saw before. Kept explicit so a rename cannot slip through as an "alias".
LEGACY_LABELS = {"asr provider", "custom vocabulary", "speaker attributes"}

#: file -> (first segments that are NOT this app's Settings, reason).
NOT_THIS_APP: dict[str, tuple[set[str], str]] = {
    "authentication/pki.md": (
        {"privacy and security", "privacy & security", "privacy"},
        "Chrome and Firefox browser settings for importing a client certificate",
    ),
    "configuration/nginx-setup.md": (
        {"general", "security"},
        "iOS and Android device Settings for trusting the server certificate",
    ),
    "installation/docker-compose.md": (
        {"resources", "certificate trust settings", "security"},
        "Docker Desktop and iOS/Android device Settings, not OpenTranscribe",
    ),
    "faq.md": (
        {"access tokens"},
        "the link text of Hugging Face's own account Settings page",
    ),
}

ARROW = r"(?:→|->|&gt;|>)"
PATH_RE = re.compile(rf"Settings\s*{ARROW}\s*(.+)")


def sidebar_labels() -> set[str]:
    en: dict[str, str] = json.loads(EN_JSON.read_text(encoding="utf-8"))
    source = MODAL.read_text(encoding="utf-8")
    keys: list[str] = []
    for line in source.splitlines():
        if "label:" in line or re.search(r"title: \$t\('settings\.sections\.", line):
            keys += re.findall(r"\$t\('([^']+)'\)", line)
    labels = {en[key].lower() for key in keys if key in en}
    return labels | LEGACY_LABELS


def starts_with_label(text: str, labels: set[str]) -> bool:
    lowered = text.lower()
    for label in labels:
        if not lowered.startswith(label):
            continue
        rest = lowered[len(label) :]
        # "Transcription & AI" starts with the label "Transcription" but names a longer,
        # different entry; a following "&" or word character means the label did not end.
        if rest == "" or not (rest[0].isalnum() or rest.lstrip().startswith("&")):
            return True
    return False


def bad_paths(text: str, labels: set[str], exempt: set[str]) -> list[str]:
    bad = []
    for line in text.splitlines():
        for match in PATH_RE.finditer(line):
            after = match.group(1).strip().lstrip("*").strip()
            if starts_with_label(after, labels):
                continue
            if any(after.lower().startswith(seg) for seg in exempt):
                continue
            bad.append(match.group(0)[:80])
    return bad


def test_sidebar_labels_are_resolved_from_the_real_modal() -> None:
    labels = sidebar_labels()
    # If the extraction silently found nothing, every doc path would "fail" for the wrong
    # reason, or (worse) a loosened regex would pass everything.
    for expected in (
        "transcription",
        "speaker identification",
        "auto-labeling (tags & collections)",
        "organization context",
        "speaker embedding system",
        "ai & chat",
    ):
        assert expected in labels, f"{expected!r} missing from {sorted(labels)}"
    assert "speech processing" not in labels
    assert "transcription & ai" not in labels


def test_checker_rejects_the_stale_paths_that_motivated_it() -> None:
    labels = sidebar_labels()
    assert (
        bad_paths(
            "in **Settings → Transcription** or Settings > Auto-Labeling (Tags", labels, set()
        )
        == []
    )
    for stale in (
        "Settings > Transcription & AI > AI Summarization Prompts",
        "Settings → Speech Processing",
        "Settings → Embeddings",
        "Settings → Search Configuration",
        "Admin Settings > Speaker Embeddings",
        "Settings → Auto-Label",
    ):
        assert bad_paths(stale, labels, set()), stale


def test_docs_name_real_settings_sections() -> None:
    labels = sidebar_labels()
    failures: list[str] = []
    for path in sorted(DOCS.rglob("*.md*")):
        rel = path.relative_to(DOCS).as_posix()
        exempt = NOT_THIS_APP.get(rel, (set(), ""))[0]
        for bad in bad_paths(path.read_text(encoding="utf-8"), labels, exempt):
            failures.append(f"{rel}: {bad!r}")
    assert not failures, (
        "docs point at Settings entries that are not in the sidebar "
        f"({len(failures)}):\n  "
        + "\n  ".join(failures)
        + f"\nValid first segments: {sorted(sidebar_labels())}"
    )
