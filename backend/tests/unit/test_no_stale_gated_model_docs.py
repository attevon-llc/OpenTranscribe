"""No operator-facing doc or script may instruct accepting a gated repo the app doesn't use.

The app gates diarization on exactly ONE HuggingFace repo:
``pyannote/speaker-diarization-community-1`` (see ``backend/app/transcription/diarizer.py``'s
``PYANNOTE_V4_MODEL`` and ``backend/app/transcription/native_provision.py``). The HuggingFace
gate is per-repo and per-account, so telling an operator to accept
``pyannote/segmentation-3.0`` / ``pyannote/speaker-diarization-3.1`` grants nothing towards
``community-1`` — an operator who follows only that advice gets a valid token that still 403s.

That old pair is not fictional, though: ``PYANNOTE_V3_FALLBACK`` in ``diarizer.py`` is a real
internal last-resort fallback, and accepting the old pair genuinely helps *that* narrow path.
So the bar here is not "the old model strings never appear anywhere" — it's "no line instructs
accepting them as if that were sufficient/required for diarization to work", which is what an
"Accept" / "agreement" / numbered how-to-fix instruction next to the old strings does, and what
a fallback-scoped mention (this file's own docstring included) does not.

Detector: for each operator-facing file, flag a line containing one of the old gated repo names
if a nearby line (within a small window) also reads like an instruction to accept/visit it as
a fix — "accept", "agree and access", "click", "visit", or a URL to the repo — UNLESS that same
window also mentions the fallback/optional framing ("fallback", "optional", "last-resort",
"internal").
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

STALE_REPOS = (
    "pyannote/segmentation-3.0",
    "pyannote/speaker-diarization-3.1",
)

# Words that, near a stale-repo mention, indicate the text is telling the operator to go get
# access to it as a fix step (rather than just mentioning it exists / describing the fallback).
INSTRUCTION_MARKERS = (
    "accept",
    "agree and access",
    "click",
    "visit",
    "huggingface.co/pyannote/segmentation-3.0",
    "huggingface.co/pyannote/speaker-diarization-3.1",
)

# Words that mean the mention is scoped to the known-legitimate internal fallback, not framed
# as a required/helpful fix for the operator's actual gate. Deliberately narrow — "optional" and
# "internal" alone are too generic (e.g. an unrelated "Optional but Recommended" heading nearby
# caused a false negative in an earlier draft of this detector) and must not suppress a finding
# on their own.
FALLBACK_MARKERS = (
    "fallback",
    "last-resort",
    "last resort",
)

WINDOW = 8  # lines of context around a stale-repo hit to search for instruction/fallback wording

# Files this repo actually ships to operators / installers. Kept short and explicit rather than
# globbing the whole tree, so a new unrelated file (e.g. a changelog entry quoting old history)
# doesn't have to reason about this detector at all.
OPERATOR_FACING_FILES = (
    "docs-site/docs/faq.md",
    "docs-site/docs/installation/docker-compose.md",
    "docs-site/docs/installation/troubleshooting.md",
    "docs-site/docs/installation/offline-installation.md",
    "docs-site/docs/installation/huggingface-setup.md",
    "README.md",
    "setup-opentranscribe.sh",
    "scripts/download-models.py",
)


def _windowed_flags(lines: list[str]) -> list[tuple[int, str]]:
    """Return (line_no, stale_repo) for every line whose window looks like a bad instruction."""
    flags: list[tuple[int, str]] = []
    lowered = [ln.lower() for ln in lines]
    for i, line in enumerate(lowered):
        hit_repo = next((r for r in STALE_REPOS if r in line), None)
        if hit_repo is None:
            continue
        lo = max(0, i - WINDOW)
        hi = min(len(lowered), i + WINDOW + 1)
        window_text = "\n".join(lowered[lo:hi])
        has_instruction = any(m in window_text for m in INSTRUCTION_MARKERS)
        has_fallback_scope = any(m in window_text for m in FALLBACK_MARKERS)
        if has_instruction and not has_fallback_scope:
            flags.append((i + 1, hit_repo))
    return flags


def _scan(path: Path) -> list[tuple[int, str]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    return _windowed_flags(text.splitlines())


@pytest.mark.parametrize("rel_path", OPERATOR_FACING_FILES)
def test_no_bad_gated_model_instruction(rel_path: str) -> None:
    path = REPO_ROOT / rel_path
    assert path.exists(), f"expected operator-facing file missing: {rel_path}"
    findings = _scan(path)
    assert not findings, (
        f"{rel_path} instructs accepting a gated repo the app does not use "
        f"(only pyannote/speaker-diarization-community-1 is actually gated on): {findings}. "
        "If this is the documented PYANNOTE_V3_FALLBACK exception, frame it with "
        "fallback/optional/last-resort wording so the detector can tell the two apart."
    )


class TestSelfTest:
    """Must-fire and must-stay-clean cases so this detector can't silently match nothing."""

    def test_must_fire_on_bare_instruction(self) -> None:
        text = (
            "1. Accept BOTH gated model agreements:\n"
            "   - [pyannote/segmentation-3.0](https://huggingface.co/pyannote/segmentation-3.0)\n"
            "   - [pyannote/speaker-diarization-3.1]"
            "(https://huggingface.co/pyannote/speaker-diarization-3.1)\n"
        )
        findings = _windowed_flags(text.splitlines())
        # Assert WHICH lines/repos fired, not merely that something did: a bare truthy check
        # passes even when the detector fires for the wrong reason on the wrong line.
        assert findings == [
            (2, "pyannote/segmentation-3.0"),
            (3, "pyannote/speaker-diarization-3.1"),
        ], f"bare accept-both instruction should flag both stale repos, got {findings}"

    def test_must_fire_on_installer_style_instruction(self) -> None:
        text = (
            "echo 'Step 1: Visit the Segmentation Model page'\n"
            "echo '   URL: https://huggingface.co/pyannote/segmentation-3.0'\n"
            "echo \"   -> Click 'Agree and access repository'\"\n"
        )
        findings = _windowed_flags(text.splitlines())
        # Only line 2 carries a stale repo; lines 1 and 3 supply the instruction wording that
        # makes it a finding. Pinning the exact tuple proves the window, not just the match.
        assert findings == [(2, "pyannote/segmentation-3.0")], (
            f"installer-style click-through instruction should flag line 2 only, got {findings}"
        )

    def test_must_stay_clean_on_fallback_framed_mention(self) -> None:
        text = (
            "The in-process PyAnnote engine keeps `speaker-diarization-3.1` as an internal\n"
            "last-resort fallback if loading community-1 fails for a non-licensing reason.\n"
            "Accepting the old pair (pyannote/segmentation-3.0, pyannote/speaker-diarization-3.1)\n"
            "is optional and only helps that inner fallback.\n"
        )
        findings = _windowed_flags(text.splitlines())
        assert not findings, f"fallback-scoped mention should not be flagged, got {findings}"

    def test_must_stay_clean_on_plain_mention_with_no_instruction(self) -> None:
        text = (
            "History: this repo used to gate on pyannote/segmentation-3.0 and\n"
            "pyannote/speaker-diarization-3.1 before switching to community-1.\n"
        )
        findings = _windowed_flags(text.splitlines())
        assert not findings, f"plain historical mention should not be flagged, got {findings}"

    def test_fires_at_least_once_across_operator_files_pre_fix(self) -> None:
        """Sanity: the detector logic itself works against a known-bad snippet shape used
        in the actual pre-fix files (regression guard for the detector, not the repo)."""
        text = (
            "2. **Accept model agreements** for:\n"
            "   - [pyannote/segmentation-3.0](https://huggingface.co/pyannote/segmentation-3.0)\n"
            "   - [pyannote/speaker-diarization-3.1]"
            "(https://huggingface.co/pyannote/speaker-diarization-3.1)\n"
            "3. **Add to .env**: `HUGGINGFACE_TOKEN=hf_xxxxxxxxxxxx`\n"
        )
        findings = _windowed_flags(text.splitlines())
        assert findings == [
            (2, "pyannote/segmentation-3.0"),
            (3, "pyannote/speaker-diarization-3.1"),
        ], f"known-bad pre-fix snippet should flag both stale repos, got {findings}"
