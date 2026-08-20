"""W2.5 — cross-meeting recurrence, the PURE half (`services/chat/recurrence.py`).

No database, no OpenSearch, no LLM: every test here drives
`recurrence.normalize_leaf`/`recurrence.detect_recurring_items` directly with
plain data, matching the module's own "pure logic" contract. The I/O half
(masking, permissions, freshness) is covered separately in
`test_chat_recurrence_service.py`.
"""

from __future__ import annotations

import pytest

from app.services.chat import recurrence

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Task 1 — the shape-tolerant normalizer
# --------------------------------------------------------------------------- #


def test_normalize_leaf_the_default_action_item_shape():
    """`{item, owner, due_date, priority, context, mentioned_timestamp}` —
    exactly what `core/default_prompts.py`'s action_items block emits."""
    raw = {
        "item": "Update the roadmap",
        "owner": "Alice",
        "due_date": "Friday",
        "priority": "high",
        "context": "Roadmap is stale",
        "mentioned_timestamp": "[01:02]",
    }
    assert recurrence.normalize_leaf(raw, recurrence.LEAF_ACTION_ITEM) == (
        "Update the roadmap",
        "Alice",
    )


def test_normalize_leaf_the_dead_schema_shape():
    """`schemas/summary.py`'s `ActionItem` — `{text, assigned_to, ..., status}`.
    Exported but nothing produces it; the normalizer must still handle it
    since `SummaryData` is `extra="allow"` and a custom prompt could emit it."""
    raw = {
        "text": "Update the roadmap",
        "assigned_to": "Alice",
        "due_date": "2026-08-21",
        "priority": "high",
        "context": "Roadmap is stale",
        "status": "pending",
    }
    assert recurrence.normalize_leaf(raw, recurrence.LEAF_ACTION_ITEM) == (
        "Update the roadmap",
        "Alice",
    )


def test_normalize_leaf_a_bare_string_follow_up():
    """`follow_up_items` genuinely are bare strings, not dicts."""
    assert recurrence.normalize_leaf("Send the pricing doc", recurrence.LEAF_FOLLOW_UP) == (
        "Send the pricing doc",
        None,
    )


def test_normalize_leaf_a_key_decision_uses_the_decision_key():
    raw = {"decision": "Adopt the new pricing model", "context": "Board approved"}
    assert recurrence.normalize_leaf(raw, recurrence.LEAF_KEY_DECISION) == (
        "Adopt the new pricing model",
        None,
    )


def test_normalize_leaf_a_fully_custom_shape_declines_rather_than_guessing():
    """A custom prompt's own field names (`task`/`who`) are not `item`/`text`/
    `description` or `owner`/`assigned_to` — SummaryData's extra="allow" makes
    this shape legitimate, and the normalizer must not guess at it."""
    raw = {"task": "Circle back with the vendor", "who": "Bob"}
    assert recurrence.normalize_leaf(raw, recurrence.LEAF_ACTION_ITEM) is None


def test_normalize_leaf_an_empty_string_declines():
    assert recurrence.normalize_leaf("   ", recurrence.LEAF_FOLLOW_UP) is None


def test_normalize_leaf_a_non_string_non_dict_declines():
    assert recurrence.normalize_leaf(42, recurrence.LEAF_ACTION_ITEM) is None


def test_normalize_leaf_keyphrase_shape():
    raw = {"phrase": "budget review", "score": 4.2, "count": 3}
    assert recurrence.normalize_leaf(raw, recurrence.LEAF_KEYPHRASE) == ("budget review", None)


# --------------------------------------------------------------------------- #
# Task 2 — candidate generation is genuinely not all-pairs
# --------------------------------------------------------------------------- #


def _item(
    file_uuid: str, text: str, leaf: str = recurrence.LEAF_ACTION_ITEM
) -> recurrence.SourceItem:
    return recurrence.SourceItem(file_uuid=file_uuid, text=text, leaf=leaf, language="en")


def _unique_word(i: int) -> str:
    """A distinct, ALL-LETTER PREFIX for index ``i`` (spreadsheet-column
    style: a, b, ..., z, aa, ab, ...), meant to be fused at the FRONT of a
    fixed nonsense suffix (``f"{_unique_word(i)}zzqux"``).

    Two traps this specifically avoids, both found empirically while writing
    this test:

    1. Fusing with NO separator matters — the tokenizer's letters-vs-digits
       split (``_TOKEN_RE`` matches a letter run and a digit run as SEPARATE
       tokens) means a naive ``f"topic{i}"`` shares the token "topic" across
       EVERY item, with only the digit varying.
    2. The varying part must lead, not trail. The Snowball English stemmer
       strips trailing inflectional letters ("topicae" and "topicas" both
       stem to "topica") — two DIFFERENT unique suffixes collided onto the
       same stemmed token in an earlier draft of this test, which was
       therefore asserting the algorithm found one group of exactly the
       "noise" size purely by test-data accident. Putting the unique part
       first means the stemmer can only ever act on the fixed, shared tail.
    """
    letters = "abcdefghijklmnopqrstuvwxyz"
    out: list[str] = []
    n = i + 1
    while n:
        n, r = divmod(n - 1, 26)
        out.append(letters[r])
    return "".join(reversed(out))


def test_candidate_generation_is_not_all_pairs():
    """200 mostly-unique items plus one recurring phrase repeated across 20
    files. All-pairs would be C(220, 2) = 24,090 comparisons; the inverted
    index must produce far fewer, because only items sharing a token are ever
    compared at all.
    """
    items: list[recurrence.SourceItem] = []
    for i in range(200):
        # Each item gets its own unique, FUSED vocabulary (see `_unique_word`)
        # — shares NOTHING with any other item, so it contributes zero
        # candidate pairs.
        w = _unique_word(i)
        items.append(_item(f"file-noise-{i}", f"{w}zzqux {w}plughy"))
    for i in range(20):
        items.append(_item(f"file-recur-{i}", "review the quarterly budget forecast"))

    result = recurrence.detect_recurring_items(items)

    all_pairs = len(items) * (len(items) - 1) // 2
    assert result.comparisons > 0, "the recurring cluster must generate SOME candidates"
    assert result.comparisons < all_pairs, (
        f"comparisons ({result.comparisons}) must be less than all-pairs ({all_pairs})"
    )
    # Much less, not just marginally: only the 20 recurring items share tokens
    # with each other, so C(20, 2) = 190 is close to the true ceiling here.
    assert result.comparisons <= 400, (
        f"comparisons ({result.comparisons}) is suspiciously close to all-pairs "
        f"({all_pairs}) for a dataset with only one overlapping cluster"
    )
    assert len(result.groups) == 1
    assert len(result.groups[0].file_uuids) == 20


# --------------------------------------------------------------------------- #
# Planted-gold precision/recall
# --------------------------------------------------------------------------- #


def test_planted_gold_precision_and_recall():
    """Three planted recurring topics, phrased differently per occurrence, plus
    a pile of one-off unique items. The detector must find exactly the three
    planted groups (recall) and invent no others (precision)."""
    items: list[recurrence.SourceItem] = []

    # Planted group A: "budget review" across 3 files, worded differently.
    items.append(_item("file-a1", "Review the quarterly budget with finance"))
    items.append(_item("file-a2", "Quarterly budget review with the finance team"))
    items.append(_item("file-a3", "Finance team to review quarterly budget numbers"))

    # Planted group B: "vendor contract renewal" across 2 files.
    items.append(_item("file-b1", "Renew the vendor contract before it expires"))
    items.append(_item("file-b2", "Vendor contract renewal is due this month"))

    # Planted group C: "onboarding checklist" across 4 files.
    for n in range(4):
        items.append(_item(f"file-c{n}", "Finish the new hire onboarding checklist"))

    # Noise: unique, non-overlapping one-off items (each its own file). Fused
    # unique words (see `_unique_word`) so no shared filler word ("topic",
    # "number", ...) accidentally pushes two noise items over the threshold.
    for i in range(10):
        w = _unique_word(i)
        items.append(_item(f"file-noise-{i}", f"{w}zzqux {w}plughy"))

    result = recurrence.detect_recurring_items(items)

    by_size = sorted(result.groups, key=lambda g: len(g.file_uuids))
    sizes = [len(g.file_uuids) for g in by_size]
    assert sizes == [2, 3, 4], f"expected planted groups of size 2, 3, 4 — got {sizes}"

    # Precision: no group spans a noise file.
    for group in result.groups:
        assert all("noise" not in uuid for uuid in group.file_uuids)

    # Recall: every planted file is accounted for in exactly one group.
    all_grouped = {uuid for g in result.groups for uuid in g.file_uuids}
    expected = (
        {f"file-a{n}" for n in (1, 2, 3)}
        | {"file-b1", "file-b2"}
        | {f"file-c{n}" for n in range(4)}
    )
    assert all_grouped == expected


# --------------------------------------------------------------------------- #
# Truncation, and item-cap disclosure
# --------------------------------------------------------------------------- #


def test_truncation_is_disclosed():
    items = [_item(f"file-{i}", f"unique thing {i}") for i in range(10)]

    result = recurrence.detect_recurring_items(items, item_cap=5)

    assert result.truncated is True
    assert result.considered == 5


def test_no_truncation_when_under_the_cap():
    items = [_item(f"file-{i}", f"unique thing {i}") for i in range(3)]

    result = recurrence.detect_recurring_items(items, item_cap=1500)

    assert result.truncated is False
    assert result.considered == 3


# --------------------------------------------------------------------------- #
# Language: zh/ja/ko decline with disclosure; Arabic WORKS
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("lang", ["zh", "ja", "ko"])
def test_no_space_scripts_decline_with_disclosure(lang: str):
    # Identical CJK text across two files WOULD be an obvious recurrence if
    # tokenized correctly — asserting it does NOT group is the point: a
    # no-space script cannot be safely tokenized into a meaningful token set.
    items = [
        recurrence.SourceItem(
            file_uuid="f1", text="讨论季度预算审查", leaf="action_items", language=lang
        ),
        recurrence.SourceItem(
            file_uuid="f2", text="讨论季度预算审查", leaf="action_items", language=lang
        ),
    ]

    result = recurrence.detect_recurring_items(items)

    assert result.groups == ()
    assert result.declined_for_language == 2
    assert result.declined_languages == (lang,)


def test_arabic_works():
    """Arabic is NOT a no-space script and has a real Snowball stemmer +
    NLTK stopword list (`textrank._SNOWBALL_LANG_MAP`/`_STOPWORD_LANG_MAP`
    both carry "ar") — recurrence detection must work over it exactly like
    any other supported language."""
    items = [
        recurrence.SourceItem(
            file_uuid="f1",
            text="مراجعة الميزانية الفصلية مع الفريق المالي",
            leaf="action_items",
            language="ar",
        ),
        recurrence.SourceItem(
            file_uuid="f2",
            text="مراجعة الميزانية الفصلية مع الفريق المالي",
            leaf="action_items",
            language="ar",
        ),
    ]

    result = recurrence.detect_recurring_items(items)

    assert result.declined_for_language == 0
    assert result.declined_languages == ()
    assert len(result.groups) == 1
    assert set(result.groups[0].file_uuids) == {"f1", "f2"}


# --------------------------------------------------------------------------- #
# D6: keyphrase-only recurrence — no LLM/summary items at all
# --------------------------------------------------------------------------- #


def test_keyphrase_only_recurrence_needs_no_llm_output():
    """No `action_items`/`key_decisions`/`follow_up_items` at all — every item
    is a `LEAF_KEYPHRASE` entry, exactly the shape a deployment with
    `LLM_PROVIDER` empty produces (`ingest_artifacts.keyphrases`, no LLM)."""
    items = [
        recurrence.SourceItem(
            file_uuid="f1", text="onboarding checklist", leaf=recurrence.LEAF_KEYPHRASE
        ),
        recurrence.SourceItem(
            file_uuid="f2", text="onboarding checklist", leaf=recurrence.LEAF_KEYPHRASE
        ),
        recurrence.SourceItem(
            file_uuid="f3", text="unrelated phrase entirely", leaf=recurrence.LEAF_KEYPHRASE
        ),
    ]

    result = recurrence.detect_recurring_items(items)

    assert len(result.groups) == 1
    assert result.groups[0].leaf == recurrence.LEAF_KEYPHRASE
    assert set(result.groups[0].file_uuids) == {"f1", "f2"}


# --------------------------------------------------------------------------- #
# Misc pure-function guards
# --------------------------------------------------------------------------- #


def test_a_single_file_repeating_the_same_item_twice_does_not_recur():
    """Recurring means CROSS-FILE. Two mentions in one meeting are one topic."""
    items = [
        _item("only-file", "call the vendor about pricing"),
        _item("only-file", "call the vendor about pricing again"),
    ]

    result = recurrence.detect_recurring_items(items)

    assert result.groups == ()


def test_jaccard_of_two_empty_sets_is_zero():
    assert recurrence.jaccard(frozenset(), frozenset()) == 0.0


def test_as_metadata_never_contains_item_text():
    items = [_item("f1", "call the vendor"), _item("f2", "call the vendor")]
    result = recurrence.detect_recurring_items(items)

    meta = result.as_metadata()

    assert "call the vendor" not in str(meta)
    assert meta["groups"] == 1
