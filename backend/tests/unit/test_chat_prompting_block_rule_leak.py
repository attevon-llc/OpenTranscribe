"""Issue #536: base rules must not leak prompt-internal block vocabulary.

`BASE_SYSTEM_RULES` describes the `<counted>`/`<overview>`/`<recurrence>`/
`<synthesis>` blocks unconditionally, because `build_system_prompt` runs
before retrieval and cannot yet know which blocks this turn will end up
with. A model once read that unconditional text on a turn with NO
`<recurrence>` block and told the user "There is no `<recurrence>` block
provided..." — prompt-internal vocabulary leaked into a user-facing answer.

`build_messages` is where presence is finally known (after
`_trim_evidence_blocks`), so it strips whichever base rule(s) reference a
block that did not survive into THIS turn's prompt — mirroring
`_OVERVIEW_ATTACHED_RULE`'s "attach only when present" shape, inverted to
"remove when absent" for rules that already ship unconditionally.

Every "rule removed" case here has a "rule kept" sibling proving the
opposite outcome, and the reverse: a turn where every block survives must
render a system message BYTE-IDENTICAL to `build_system_prompt`'s raw
output — this fix must not reword or reorder anything a turn already saw.
"""

from __future__ import annotations

from app.services.chat.prompting import _BLOCK_RULES
from app.services.chat.prompting import _RULE_10_COUNTED
from app.services.chat.prompting import _RULE_11_LANGUAGE
from app.services.chat.prompting import _RULE_12_OVERVIEW
from app.services.chat.prompting import _RULE_13_RECURRENCE
from app.services.chat.prompting import _RULE_14_OVERVIEW_FOCUS
from app.services.chat.prompting import _RULE_15_SYNTHESIS
from app.services.chat.prompting import BASE_SYSTEM_RULES
from app.services.chat.prompting import build_messages
from app.services.chat.prompting import build_system_prompt

_LEAK_TOKENS = ("<counted", "<overview", "<recurrence")

_COUNTED = "<counted>\ntotal: 3\n</counted>\n\n"
_OVERVIEW = "<overview>\nrecordings: 2\n</overview>\n\n"
_RECURRENCE = "<recurrence>\nrecurring items: 1\n</recurrence>\n\n"
_SYNTHESIS = "<synthesis>\ndraft answer\n</synthesis>\n\n"


def _system_message(**block_kwargs) -> str:
    """Run `build_messages` with a roomy budget and return the system content."""
    messages, _ids = build_messages(
        system_prompt=BASE_SYSTEM_RULES,
        chunks=[],
        history=[],
        question="what happened?",
        context_window=100_000,
        response_tokens=500,
        **block_kwargs,
    )
    assert messages[0]["role"] == "system"
    return messages[0]["content"]


# --------------------------------------------------------------------------- #
# Structural: `_BLOCK_RULES` names exactly the block-referencing rules
# --------------------------------------------------------------------------- #


def test_block_rules_map_covers_the_four_evidence_blocks():
    assert set(_BLOCK_RULES) == {"counted", "overview", "recurrence", "synthesis"}
    assert _BLOCK_RULES["counted"] == (_RULE_10_COUNTED,)
    assert _BLOCK_RULES["overview"] == (_RULE_12_OVERVIEW, _RULE_14_OVERVIEW_FOCUS)
    assert _BLOCK_RULES["recurrence"] == (_RULE_13_RECURRENCE,)
    assert _BLOCK_RULES["synthesis"] == (_RULE_15_SYNTHESIS,)


def test_the_language_rule_names_no_block_and_is_never_in_the_map():
    """Rule 11 is a global instruction (answer in the question's language) —
    it must never be treated as block-specific, or a turn with no evidence
    blocks at all would lose it too."""
    assert _BLOCK_RULES, "the map must not be empty, or the loop below checks nothing"
    for rules in _BLOCK_RULES.values():
        assert _RULE_11_LANGUAGE not in rules


# --------------------------------------------------------------------------- #
# (a) present when the block is present
# --------------------------------------------------------------------------- #


def test_rule_10_present_when_counted_block_survives():
    content = _system_message(counted_block=_COUNTED)
    assert _RULE_10_COUNTED in content


def test_rules_12_and_14_present_when_overview_block_survives():
    content = _system_message(overview_block=_OVERVIEW)
    assert _RULE_12_OVERVIEW in content
    assert _RULE_14_OVERVIEW_FOCUS in content


def test_rule_13_present_when_recurrence_block_survives():
    content = _system_message(recurrence_block=_RECURRENCE)
    assert _RULE_13_RECURRENCE in content


def test_rule_15_present_when_synthesis_block_survives():
    content = _system_message(synthesis_block=_SYNTHESIS)
    assert _RULE_15_SYNTHESIS in content


def test_language_rule_11_is_always_present_regardless_of_blocks():
    """Rule 11 is not block-gated — it must survive with or without evidence
    blocks, including the empty-scope case exercised below."""
    assert _RULE_11_LANGUAGE in _system_message()
    assert _RULE_11_LANGUAGE in _system_message(
        counted_block=_COUNTED,
        overview_block=_OVERVIEW,
        recurrence_block=_RECURRENCE,
        synthesis_block=_SYNTHESIS,
    )


# --------------------------------------------------------------------------- #
# (b) absent when the block is absent
# --------------------------------------------------------------------------- #


def test_rule_10_absent_without_a_counted_block():
    content = _system_message()
    assert _RULE_10_COUNTED not in content
    assert "<counted" not in content


def test_rules_12_and_14_absent_without_an_overview_block():
    content = _system_message()
    assert _RULE_12_OVERVIEW not in content
    assert _RULE_14_OVERVIEW_FOCUS not in content
    assert "<overview" not in content


def test_rule_13_absent_without_a_recurrence_block():
    content = _system_message()
    assert _RULE_13_RECURRENCE not in content
    assert "<recurrence" not in content


def test_rule_15_absent_without_a_synthesis_block():
    content = _system_message()
    assert _RULE_15_SYNTHESIS not in content
    assert "<synthesis" not in content


def test_overview_rules_absent_when_the_block_is_dropped_by_the_budget():
    """A block can be non-empty on input and still not reach the prompt —
    `_trim_evidence_blocks` drops it whole when it cannot fit. The rule must
    follow the block, not the caller's input, or it addresses a block the
    model was never shown (the exact #536 shape)."""
    huge_overview = "<overview>" + ("recording detail. " * 400) + "</overview>"
    messages, _ids = build_messages(
        system_prompt=BASE_SYSTEM_RULES,
        chunks=[],
        history=[],
        question="what were the key decisions across the meetings?",
        # Tiny window: the overview cannot fit and must be dropped.
        context_window=200,
        response_tokens=50,
        overview_block=huge_overview,
    )
    content = messages[0]["content"]
    assert _RULE_12_OVERVIEW not in content
    assert _RULE_14_OVERVIEW_FOCUS not in content
    assert "<overview" not in content
    # And the block itself really was dropped, not merely small — the user
    # message must not carry it either.
    assert "<overview>" not in messages[-1]["content"]


def test_removing_one_block_rule_leaves_the_others_untouched():
    """Independence: dropping counted must not disturb overview's rules, or
    any other combination — this is the shape a naive single string-strip
    could get wrong if it over-matched."""
    content = _system_message(overview_block=_OVERVIEW)
    # counted/recurrence/synthesis all absent, overview present.
    assert _RULE_10_COUNTED not in content
    assert _RULE_13_RECURRENCE not in content
    assert _RULE_15_SYNTHESIS not in content
    assert _RULE_12_OVERVIEW in content
    assert _RULE_14_OVERVIEW_FOCUS in content
    # Every non-block-specific rule (1-9, 11) is untouched.
    for rule_num in range(1, 10):
        assert f"\n{rule_num}. " in content
    assert _RULE_11_LANGUAGE in content


# --------------------------------------------------------------------------- #
# (c) must-fire leak guard — the literal bug report
# --------------------------------------------------------------------------- #


def test_no_block_vocabulary_leaks_when_no_evidence_blocks_are_present():
    """The exact scenario from the bug report: a turn with none of the four
    evidence blocks must never mention their tag names anywhere the model —
    and therefore the user — can read them."""
    messages, _ids = build_messages(
        system_prompt=BASE_SYSTEM_RULES,
        chunks=[],
        history=[],
        question="what did we cover?",
        context_window=100_000,
        response_tokens=500,
    )
    full_prompt = "".join(m["content"] for m in messages)
    for token in _LEAK_TOKENS:
        assert token not in full_prompt, f"{token!r} leaked with no matching block present"
    assert "<synthesis" not in full_prompt


def test_no_context_mode_never_carried_the_leak_either():
    """Sanity control: `NO_CONTEXT_SYSTEM_RULES` never had this text at all,
    so stripping must be a no-op there rather than erroring."""
    from app.services.chat.prompting import NO_CONTEXT_SYSTEM_RULES

    messages, _ids = build_messages(
        system_prompt=NO_CONTEXT_SYSTEM_RULES,
        chunks=[],
        history=[],
        question="hello",
        context_window=8192,
        response_tokens=500,
    )
    assert messages[0]["content"] == NO_CONTEXT_SYSTEM_RULES


# --------------------------------------------------------------------------- #
# Byte-identical when every block survives
# --------------------------------------------------------------------------- #


def test_system_message_is_byte_identical_to_build_system_prompt_when_all_blocks_survive():
    """The other half of the invariant: a turn that DOES have every block
    must read exactly as it did before this fix — no rewording, no
    reordering, nothing stripped."""
    system_prompt = build_system_prompt(use_context=True)
    content = _system_message(
        counted_block=_COUNTED,
        overview_block=_OVERVIEW,
        recurrence_block=_RECURRENCE,
        synthesis_block=_SYNTHESIS,
    )
    assert content == system_prompt
    assert content == BASE_SYSTEM_RULES


def test_system_message_with_only_counted_present_is_still_byte_identical_system_text():
    """Not just "all or nothing" — any single present block must leave the
    SYSTEM text (as opposed to the stripped rules) untouched too."""
    content = _system_message(counted_block=_COUNTED)
    # Only rules 12/13/14/15 were removed; nothing else in the string moved.
    expected = BASE_SYSTEM_RULES
    for rule in (
        _RULE_12_OVERVIEW,
        _RULE_13_RECURRENCE,
        _RULE_14_OVERVIEW_FOCUS,
        _RULE_15_SYNTHESIS,
    ):
        expected = expected.replace("\n" + rule, "")
    assert content == expected
