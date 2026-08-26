"""Empty env vars must fall back to coded defaults, not crash (issue: fresh-install rehearsal).

``.env.example`` ships dozens of deliberate ``VAR=`` blanks meaning "use the coded default" — a
convention used throughout that file. Pydantic's own parsers reject an empty string outright for
bool/int/float fields, so before ``env_ignore_empty=True`` was added to ``Settings.model_config``,
any deployment that copied ``.env.example`` verbatim and left one of those blanks in place would
crash at import time the moment that field's env var was read. This was caught live: a real
fresh-install rehearsal crashed on ``MFA_REQUIRE_REDIS=`` — a bool field an earlier, narrower
int/float-only sweep (commit 5f9f2ffd) missed entirely because it only patched the int/float
crash class, not bool.

``env_ignore_empty`` fixes the whole class at once: an empty env var is treated as unset, and the
field's plain Python default applies — no per-field workaround needed. This file proves that
holds for a representative sample of field TYPES (bool, int, float, `int | None`) and that a
genuinely malformed (non-empty) value still raises loudly, which is a deliberate, retained
contract (see ``NUM_SPEAKERS`` in ``config.py``), not a gap this fix should paper over.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.core.config import Settings

_PRINT_FIELD_TEMPLATE = "from app.core.config import Settings; print(Settings().{field})"

# (field, coded default as it will print, env var name -- identical to field name for
# every case here) covering bool, int, float, and int | None field types.
#
# NOTE on the two bool cases: MFA_REQUIRE_REDIS and PKI_REVOCATION_SOFT_FAIL are the
# only bool fields chosen here, deliberately -- both were rebuilt as `bool | None = None`
# literals resolved post-construction (Group E). Every OTHER bool field in this file
# still computes its default as `os.getenv("FIELD", "true").lower() == "true"` in the
# class body, evaluated ONCE at import time using the process's actual os.environ at
# that moment. If such a field's env var is ALSO blank at import (not just at
# `Settings()` construction), that class-body expression itself resolves to the wrong
# value -- `env_ignore_empty` only fixes pydantic-settings' env-sourcing at
# `Settings()` construction, not this earlier, separate default-computation step. None
# of those other fields ship blank in `.env.example` today (see
# test_every_env_example_blank_var_constructs below), so this is a latent, out-of-scope
# landmine rather than a live bug -- flagged here rather than silently worked around.
_REPRESENTATIVE_FIELDS = [
    ("MFA_REQUIRE_REDIS", "True"),  # bool | None, resolved post-construction (Group E)
    ("PKI_REVOCATION_SOFT_FAIL", "False"),  # bool | None, resolved post-construction (Group E)
    ("SMTP_PORT", "587"),  # plain int
    ("PBKDF2_ITERATIONS", "210000"),  # plain int
    ("MAX_SPEAKERS", "20"),  # plain int
    ("SEARCH_HYBRID_MIN_SCORE", "0.005"),  # plain float
    ("NUM_SPEAKERS", "None"),  # int | None
    ("MAX_UPLOAD_BYTES", str(15 * 1024 * 1024 * 1024)),  # int | None, field_validator normalized
]


def test_model_config_ignores_empty_env() -> None:
    assert Settings.model_config["env_ignore_empty"] is True


@pytest.mark.parametrize(("field", "expected_default"), _REPRESENTATIVE_FIELDS)
def test_empty_env_values_fall_back_to_defaults(
    run_in_clean_process, tmp_path, field: str, expected_default: str
) -> None:
    out = run_in_clean_process(
        _PRINT_FIELD_TEMPLATE.format(field=field),
        UPLOAD_DIR=str(tmp_path / "up"),
        TEMP_DIR=str(tmp_path / "tmp"),
        # MFA_REQUIRE_REDIS's default depends on ENVIRONMENT; pin it explicitly so
        # this test is not coupled to whichever default `run_in_clean_process` uses.
        ENVIRONMENT="production",
        **{field: ""},
    )
    assert out == expected_default


def test_every_env_example_blank_var_constructs(run_in_clean_process, tmp_path) -> None:
    """The exact scenario that crashed: every deliberately-blank `.env.example` var, empty."""
    env_example = Path(__file__).parents[3] / ".env.example"
    assert env_example.is_file(), env_example

    blank_vars: dict[str, str] = {}
    for line in env_example.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=$", stripped)
        if m:
            blank_vars[m.group(1)] = ""

    # Sanity: this must actually find a nontrivial number of blanks, or the test proves
    # nothing (an empty .env.example, or one that stopped using the blank convention,
    # would make this pass vacuously).
    assert len(blank_vars) >= 8, blank_vars

    out = run_in_clean_process(
        "from app.core.config import Settings; Settings(); print('OK')",
        UPLOAD_DIR=str(tmp_path / "up"),
        TEMP_DIR=str(tmp_path / "tmp"),
        **blank_vars,
    )
    assert out == "OK"


def test_malformed_non_empty_value_still_raises(run_in_clean_process, tmp_path) -> None:
    """A genuinely malformed (non-empty) value must still fail loudly -- not degrade silently."""
    with pytest.raises(AssertionError):
        run_in_clean_process(
            "from app.core.config import Settings; Settings()",
            UPLOAD_DIR=str(tmp_path / "up"),
            TEMP_DIR=str(tmp_path / "tmp"),
            NUM_SPEAKERS="20 # comment",
        )
