"""Behaviour in ``redaction/spans.py`` that no test asserted (issue #446).

⚠️ **These tests do NOT lower the mutation score, and the docstring said they
would until it was measured.** All 10 surviving mutants in this module are
EQUIVALENT — they cannot change observable behaviour, so no test can kill them.
That was established by writing these tests, re-running the module (with the file
correctly registered in ``MODULE_TESTS[spans]``, and coverage at 98%), and
finding the count unmoved at 10.

Why each is equivalent — worth recording so the next person does not re-spend the
afternoon:

* ``style: str = "label"`` default → ``"LABEL"`` / ``"XXlabelXX"`` (mutants 1, 2).
  Any value failing ``style not in VALID_STYLES`` is immediately normalised back
  to ``"label"``, so a corrupted default repairs itself one line later.
* The fallback ``style = "label"`` → ``None`` / ``"LABEL"`` / ``"XXlabelXX"``
  (7, 8, 9). ``_placeholder`` dispatches on ``asterisks`` / ``first_letter`` /
  ``blur`` and **falls through to the label form for anything else**, including
  ``None``.
* ``not text or not spans`` → ``and`` (3). With ``and``, a non-empty text with no
  spans stops short-circuiting and runs the whole path — reaching ``if not
  applied: return text, []``, the same answer by a longer route.
* ``char_start > cursor`` → ``>=`` (59) and ``cursor < n`` → ``<=`` (69). At
  equality the extra branch appends ``text[cursor:cursor]`` / ``text[n:]``, the
  empty string, so the joined output is byte-identical.
* ``build_word_offsets``' **first** ``idx < 0`` → ``<= 0`` / ``< 1`` (23, 24).
  When ``find`` returns 0 the mutant runs the fallback ``text.find(token)``,
  which returns 0 as well. Both checks are on the first guard, not the second —
  a mutation on the *second* guard would be killable, and mutmut did not generate
  one.

So **10 is a hard floor for this module**: it is as well-tested as mutation
testing can measure, and further effort here buys nothing. `spans` should not be
re-triaged without first re-reading this list.

The tests below are kept anyway, and are not vacuous — each asserts real
behaviour that genuinely had no assertion (the default style, the invalid-style
fallback, both empty-input paths, and word offsets at position 0). They would
catch a regression that *did* change behaviour; they simply cannot catch a
mutation that does not.
"""

from __future__ import annotations

import pytest

from app.services.redaction.spans import RedactionSpan
from app.services.redaction.spans import apply_redactions
from app.services.redaction.spans import build_word_offsets

_TEXT = "Call Dana Whitfield about the invoice."
#: Covers "Dana Whitfield".
_NAME_SPAN = RedactionSpan(char_start=5, char_end=19, category="pii", entity_type="PERSON")


def test_the_default_style_is_label() -> None:
    """Calling without ``style`` must mask the same way as ``style="label"``.

    Every existing test passed `style=` explicitly, so the default — which is
    what production callers use — was never exercised at all.

    This does not kill the default-argument mutants: a corrupted default fails
    `style not in VALID_STYLES` and is normalised straight back to "label".
    It does pin that the default IS the label style, which is the contract
    callers depend on and which nothing else asserts.
    """
    implicit, _ = apply_redactions(_TEXT, [_NAME_SPAN])
    explicit, _ = apply_redactions(_TEXT, [_NAME_SPAN], style="label")

    assert implicit == explicit
    assert "Dana Whitfield" not in implicit, "the name survived the default style"
    assert implicit != _TEXT


@pytest.mark.parametrize("bogus", ["not-a-style", "", "LABEL", "Label"])
def test_an_unrecognised_style_falls_back_to_label(bogus: str) -> None:
    """`style not in VALID_STYLES` must normalise to ``label``, not to anything else.

    The case-variant inputs matter: the check is
    a membership test, so ``"LABEL"`` is NOT valid and must be normalised — a
    fallback that assigned ``None`` would reach `_placeholder` with a style it
    cannot dispatch.

    Whatever it does, it must still MASK: the failure that matters here is not a
    wrong placeholder but a bad style causing the original text to pass through.
    """
    masked, applied = apply_redactions(_TEXT, [_NAME_SPAN], style=bogus)
    expected, _ = apply_redactions(_TEXT, [_NAME_SPAN], style="label")

    assert masked == expected
    assert "Dana Whitfield" not in masked, f"style={bogus!r} let the name through"
    assert len(applied) == 1


def test_empty_spans_returns_the_text_untouched() -> None:
    """`not text or not spans` — the OR is load-bearing.

    With `and`, a non-empty text with NO spans stops short-circuiting and runs the whole masking path; it happens to reach
    the same answer via the `if not applied` guard further down, so this asserts
    the OBSERVABLE contract rather than which branch produced it: unmasked text
    and an empty applied list, with the SAME string object semantics callers
    rely on.
    """
    masked, applied = apply_redactions(_TEXT, [])

    assert masked == _TEXT
    assert applied == []


def test_empty_text_with_spans_returns_empty() -> None:
    """The other half of the same OR, so neither operand can be dropped."""
    masked, applied = apply_redactions("", [_NAME_SPAN])

    assert masked == ""
    assert applied == []


def test_a_word_at_offset_zero_gets_its_real_span() -> None:
    """`idx < 0` must not become `idx < 1` on the not-found check.

    This is the one boundary here with teeth. `text.find` returns -1 for "not
    found" and 0 for "found at the start". Treating 0 as not-found makes the
    FIRST WORD of every transcript collapse to a zero-width span at the cursor —
    and `map_char_span_to_words` intersects on `we > char_start and ws <
    char_end`, so a zero-width span intersects nothing. A redaction covering the
    first word would map to no words at all and silently fail to mask it in any
    word-indexed consumer.

    No existing test asserted the offset of a token at position 0 — the most
    common position there is. (The surviving mutants sit on the FIRST guard,
    where the fallback re-finds the same index, so this pins the behaviour
    without moving the mutation score.)
    """
    text = "Dana called about the invoice."
    words = [{"word": "Dana"}, {"word": "called"}, {"word": "about"}]

    offsets = build_word_offsets(text, words)

    assert offsets[0] == (0, 4), "the first word must span [0, 4), not a zero-width point"
    assert offsets[1] == (5, 11)
    assert offsets[2] == (12, 17)
    # The reason it matters, asserted rather than described.
    assert text[offsets[0][0] : offsets[0][1]] == "Dana"


def test_a_genuinely_missing_word_still_degrades_to_a_point() -> None:
    """The control for the above: not-found must STILL produce a zero-width span.

    Without this, "make offsets[0] non-zero-width" could be satisfied by deleting
    the not-found branch entirely, which would raise or mis-align on real ASR
    output where a token was normalised away.
    """
    offsets = build_word_offsets("Dana called.", [{"word": "Dana"}, {"word": "absent"}])

    assert offsets[0] == (0, 4)
    assert offsets[1][0] == offsets[1][1], "a missing token must collapse to a point"
