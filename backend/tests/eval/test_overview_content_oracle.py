"""``scripts/overview_content_oracle.py`` (#532 follow-up, Unit U9) — pure scoring glue.

Loaded via ``importlib``, same convention as ``test_build_probe_question_set.py``: the
module lives under ``scripts/`` and is never installed as a package. Nothing here touches
Postgres or ``docker exec`` — :func:`fetch_files` and :func:`corpus_preconditions` are never
called. Only the pure functions (:func:`build_candidates`, :func:`score_question_set`,
:func:`apply_rules`) are exercised, against small in-memory fixtures standing in for what a
real ``fetch_files`` row and a real U8 question would look like.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _module():
    path = Path(__file__).resolve().parents[3] / "scripts" / "overview_content_oracle.py"
    spec = importlib.util.spec_from_file_location("overview_content_oracle_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


oco = _module()


@pytest.fixture
def app():
    return oco._load_app_functions()


def _digest(sections: list[str]) -> dict:
    return {
        "sections": [
            {"index": i, "text": text, "start_time": i * 60.0} for i, text in enumerate(sections)
        ]
    }


def _file_row(
    *,
    title: str = "QMSum Product — TS3005a",
    sections: list[str] | None = None,
    brief: str = "The team discussed the remote control design.",
    decisions: list | None = None,
    actions: list | None = None,
) -> dict:
    summary_data: dict = {"brief_summary": brief}
    if decisions is not None:
        summary_data["key_decisions"] = decisions
    if actions is not None:
        summary_data["action_items"] = actions
    return {
        "title": title,
        "summary_status": "completed",
        "summary_data": summary_data,
        "digest": _digest(
            sections
            if sections is not None
            else ["Opening remarks.", "Middle discussion.", "Closing decisions made here."]
        ),
        "source_fingerprint": "fp-1",
    }


# ---------------------------------------------------------------------------
# build_candidates
# ---------------------------------------------------------------------------


def test_build_candidates_c_is_the_leading_up_to_three_sections(app):
    row = _file_row(sections=["S0 text.", "S1 text.", "S2 text.", "S3 text."])
    candidates = oco.build_candidates(row, n_files_in_scope=1, app=app)
    assert candidates["C"] == "S0 text. S1 text. S2 text."


def test_build_candidates_slast_is_the_closing_section(app):
    row = _file_row(sections=["S0 text.", "S1 text.", "S2 text.", "S3 text."])
    candidates = oco.build_candidates(row, n_files_in_scope=1, app=app)
    assert candidates["Slast"] == "S3 text."


def test_build_candidates_s0_and_smid_and_slast_differ_on_a_four_section_file(app):
    row = _file_row(sections=["S0 text.", "S1 text.", "S2 text.", "S3 text."])
    candidates = oco.build_candidates(row, n_files_in_scope=1, app=app)
    assert candidates["S0"] == "S0 text."
    assert candidates["Smid"] == "S2 text."  # len(sections)//2 == 2
    assert candidates["Slast"] == "S3 text."
    assert len({candidates["S0"], candidates["Smid"], candidates["Slast"]}) == 3


def test_build_candidates_p_is_the_plain_highlight_pi_includes_items(app):
    row = _file_row(
        brief="Lead paragraph.",
        decisions=[{"decision": "Ship on Friday"}],
        actions=[{"item": "Send the report"}],
    )
    candidates = oco.build_candidates(row, n_files_in_scope=1, app=app)
    assert candidates["P"] == "Lead paragraph."
    assert "Ship on Friday" in candidates["PI"]
    assert "Send the report" in candidates["PI"]
    assert "Ship on Friday" not in candidates["P"], "P must never carry items, only PI does"


def test_build_candidates_h1_is_p_plus_closing_h2_is_pi_plus_closing(app):
    row = _file_row(
        sections=["Opening.", "Middle.", "The closing section text."],
        brief="Lead paragraph.",
        decisions=[{"decision": "Ship on Friday"}],
    )
    candidates = oco.build_candidates(row, n_files_in_scope=1, app=app)
    assert "Lead paragraph." in candidates["H1"]
    assert "The closing section text." in candidates["H1"]
    assert "Ship on Friday" not in candidates["H1"], "H1 uses P, never PI"
    assert "Lead paragraph." in candidates["H2"]
    assert "Ship on Friday" in candidates["H2"]
    assert "The closing section text." in candidates["H2"]


def test_build_candidates_an_empty_digest_yields_empty_section_candidates(app):
    row = _file_row(sections=[])
    candidates = oco.build_candidates(row, n_files_in_scope=1, app=app)
    assert candidates["C"] == ""
    assert candidates["S0"] == candidates["Smid"] == candidates["Slast"] == ""
    # H1/H2 degrade to the abstractive half alone with no closing text appended.
    assert candidates["H1"] == candidates["P"]


# ---------------------------------------------------------------------------
# score_question_set
# ---------------------------------------------------------------------------


def test_score_question_set_scopes_reference_items_to_their_own_file(app):
    """MUST-FIRE-shaped: a reference item tagged for file B must never count toward
    file A's recall, even when both are in the same question's scope — otherwise a
    correct answer about A registers as content coverage of B too."""
    questions = [
        {
            "label": "multi-TS3005-decisions",
            "category": "multi_file",
            "file_uuids": ["uuid-a", "uuid-b"],
            "reference": "[TS3005a] the design uses a rubber casing\n[TS3005b] the battery lasts ten hours",
        }
    ]
    files = {
        "uuid-a": _file_row(
            title="QMSum Product — TS3005a",
            brief="the design uses a rubber casing for durability",
        ),
        "uuid-b": _file_row(
            title="QMSum Product — TS3005b",
            brief="completely unrelated content about something else",
        ),
    }
    summary = oco.score_question_set(questions, files, app)
    # Only file A's own item can be recalled by file A's candidates; file B's brief
    # does not mention the battery, so P's pooled recall must be < 100%.
    p_bucket = summary["by_composition"]["P"]
    assert p_bucket["items_total"] == 2  # one item scored per file (A's item, B's item)
    assert p_bucket["items_recalled"] == 1  # only A's own item, from A's own text


def test_score_question_set_skips_a_question_with_no_reference():
    questions = [
        {
            "label": "multi-X-decisions",
            "category": "multi_file",
            "file_uuids": ["uuid-a"],
            "reference": None,
        }
    ]
    summary = oco.score_question_set(questions, {"uuid-a": _file_row()}, oco._load_app_functions())
    assert summary["skipped_no_reference"] == 1
    assert summary["scored_file_question_pairs"] == 0


def test_score_question_set_skips_a_file_with_no_fetched_row():
    questions = [
        {
            "label": "multi-TS3005-decisions",
            "category": "multi_file",
            "file_uuids": ["uuid-missing"],
            "reference": "[TS3005a] some item",
        }
    ]
    summary = oco.score_question_set(questions, {}, oco._load_app_functions())
    assert summary["skipped_no_file_row"] == 1


# ---------------------------------------------------------------------------
# apply_rules
# ---------------------------------------------------------------------------


def _bucket(items_recalled, items_total, files_covered, files_total):
    return {
        "items_recalled": items_recalled,
        "items_total": items_total,
        "pooled_recall": items_recalled / items_total if items_total else None,
        "files_covered": files_covered,
        "files_total": files_total,
        "per_file_coverage": files_covered / files_total if files_total else None,
        "mean_chars": 100,
        "median_chars": 100,
    }


def _summary_with(**overrides):
    base = {
        "C": _bucket(10, 100, 5, 20),
        "S0": _bucket(2, 100, 1, 20),
        "Smid": _bucket(2, 100, 1, 20),
        "Slast": _bucket(15, 100, 8, 20),
        "P": _bucket(20, 100, 10, 20),
        "PI": _bucket(22, 100, 11, 20),
        "H1": _bucket(25, 100, 12, 20),
        "H2": _bucket(27, 100, 13, 20),
    }
    base.update(overrides)
    return {"by_composition": base, "by_shape": {}, "scored_file_question_pairs": 1}


def test_r_sec_keeps_slast_when_it_is_the_best_section():
    rules = oco.apply_rules(_summary_with())
    assert rules["r_sec"]["chosen_section"] == "Slast"
    assert rules["r_sec"]["overturned"] is False


def test_r_sec_overturns_to_the_better_alternative_when_the_threshold_is_cleared():
    """MUST-FIRE: Smid clears BOTH the 20% relative AND the 5-item absolute bar over
    Slast — the pre-registered rule must actually overturn the default choice."""
    summary = _summary_with(Slast=_bucket(5, 100, 2, 20), Smid=_bucket(15, 100, 8, 20))
    rules = oco.apply_rules(summary)
    assert rules["r_sec"]["overturned"] is True
    assert rules["r_sec"]["chosen_section"] == "Smid"


def test_r_sec_does_not_overturn_on_relative_gain_alone_without_the_item_floor():
    """MUST-STAY-CLEAN: Smid beats Slast by >20% relative but the absolute item
    delta is below 5 — must NOT overturn (both conditions are required)."""
    summary = _summary_with(Slast=_bucket(10, 1000, 2, 20), Smid=_bucket(13, 1000, 2, 20))
    rules = oco.apply_rules(summary)
    assert rules["r_sec"]["overturned"] is False


def test_r_items_ships_pi_when_it_clears_the_five_item_floor():
    rules = oco.apply_rules(_summary_with(PI=_bucket(30, 100, 15, 20), P=_bucket(20, 100, 10, 20)))
    assert rules["r_items"]["ship"] == "PI"
    assert rules["r_items"]["delta_items"] == 10


def test_r_items_ships_p_when_pi_does_not_clear_the_floor():
    """MUST-STAY-CLEAN control: PI ahead of P by fewer than 5 items must ship the
    literal pre-registered 'paragraph + 1 section' shape, not the items-included one."""
    rules = oco.apply_rules(_summary_with(PI=_bucket(21, 100, 10, 20), P=_bucket(20, 100, 10, 20)))
    assert rules["r_items"]["ship"] == "P"


def test_k0_does_not_trigger_when_the_hybrid_beats_the_control():
    rules = oco.apply_rules(_summary_with())
    assert rules["k0"]["triggered"] is False


def test_k0_triggers_when_the_hybrid_is_materially_worse_on_both_axes():
    """MUST-FIRE: the kill switch must actually fire when both coverage AND recall
    fall below 75% of the control — this is the STOP-spend-no-GPU-time case."""
    summary = _summary_with(
        C=_bucket(50, 100, 15, 20),
        H1=_bucket(5, 100, 2, 20),
        H2=_bucket(5, 100, 2, 20),
        PI=_bucket(6, 100, 2, 20),
        P=_bucket(20, 100, 10, 20),
    )
    rules = oco.apply_rules(summary)
    assert rules["k0"]["triggered"] is True


def test_k0_does_not_trigger_on_only_one_axis_being_worse():
    """MUST-STAY-CLEAN: coverage below 75% but recall still healthy must NOT trigger
    the kill switch — both axes are required, per the plan's own wording."""
    summary = _summary_with(
        C=_bucket(50, 1000, 15, 20),
        H2=_bucket(45, 1000, 2, 20),  # coverage far below 75% of control's...
        PI=_bucket(6, 100, 2, 20),
        P=_bucket(20, 100, 10, 20),
    )
    rules = oco.apply_rules(summary)
    # recall_ratio stays high (45/50 = 0.9 >= 0.75) even though coverage collapsed,
    # so K0 must not fire.
    assert rules["k0"]["triggered"] is False
