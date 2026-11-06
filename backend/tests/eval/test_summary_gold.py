"""Tests for ``nxt_summaries``: the AMI/ICSI gold decisions and actions and their times.

The unit tests build tiny NXT files by hand. The NAS-gated test reproduces the counts the
quality-evaluation plan quotes for the three gate meetings, so a change to the reader that
silently loses links shows up as a different number.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.scripts.corpus_injection.nxt import strip_markers
from app.scripts.corpus_injection.nxt_summaries import SCORED_LAYERS
from app.scripts.corpus_injection.nxt_summaries import CorpusLayout
from app.scripts.corpus_injection.nxt_summaries import ami_layout
from app.scripts.corpus_injection.nxt_summaries import icsi_layout
from app.scripts.corpus_injection.nxt_summaries import read_gold_items

NITE = 'xmlns:nite="http://nite.sourceforge.net/"'
NAS = Path("/mnt/nas/opentranscribe-benchmarks")


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'<?xml version="1.0" encoding="ISO-8859-1"?>\n{body}', encoding="latin-1")


def _abssumm(meeting: str) -> str:
    return f"""<nite:root {NITE}>
<abstract nite:id="{meeting}.abstract.1">
<sentence nite:id="{meeting}.s.1">The group met.</sentence>
</abstract>
<actions nite:id="{meeting}.actions.1">
<sentence nite:id="{meeting}.s.2">Alice will send   the budget.</sentence>
</actions>
<decisions nite:id="{meeting}.decisions.1">
<sentence nite:id="{meeting}.s.3">The remote will be yellow.</sentence>
<sentence nite:id="{meeting}.s.4">The team will skip teletext.</sentence>
<sentence nite:id="{meeting}.s.5">Nobody linked this one.</sentence>
</decisions>
<problems nite:id="{meeting}.problems.1">
<sentence nite:id="{meeting}.s.6">Battery type is open.</sentence>
</problems>
</nite:root>"""


def _build_ami(root: Path, meeting: str = "ES9999a") -> None:
    _write(root / "abstractive" / f"{meeting}.abssumm.xml", _abssumm(meeting))
    # s.3 is supported by two acts on two channels: its time is the EARLIER of the two.
    _write(
        root / "extractive" / f"{meeting}.summlink.xml",
        f"""<nite:root {NITE}>
<summlink nite:id="l1">
<nite:pointer role="extractive" href="{meeting}.B.dialog-act.xml#id({meeting}.B.dialog-act.x.1)"/>
<nite:pointer role="abstractive" href="{meeting}.abssumm.xml#id({meeting}.s.3)"/>
</summlink>
<summlink nite:id="l2">
<nite:pointer role="extractive" href="{meeting}.A.dialog-act.xml#id({meeting}.A.dialog-act.x.2)"/>
<nite:pointer role="abstractive" href="{meeting}.abssumm.xml#id({meeting}.s.3)"/>
</summlink>
<summlink nite:id="l3">
<nite:pointer role="extractive" href="{meeting}.A.dialog-act.xml#id({meeting}.A.dialog-act.x.3)"/>
<nite:pointer role="abstractive" href="{meeting}.abssumm.xml#id({meeting}.s.4)"/>
</summlink>
<summlink nite:id="l4">
<nite:pointer role="extractive" href="{meeting}.A.dialog-act.xml#id({meeting}.A.dialog-act.x.9)"/>
<nite:pointer role="abstractive" href="{meeting}.abssumm.xml#id({meeting}.s.2)"/>
</summlink>
</nite:root>""",
    )
    # Channel A: a vocalsound with a time opens the first act's range; act 3 is a single word.
    _write(
        root / "words" / f"{meeting}.A.words.xml",
        f"""<nite:root {NITE}>
<vocalsound nite:id="{meeting}.A.words0" starttime="10.0" endtime="10.5" type="other"/>
<w nite:id="{meeting}.A.words1" starttime="10.5" endtime="11.0">hello</w>
<w nite:id="{meeting}.A.words2" starttime="60.25" endtime="61.0">yellow</w>
<w nite:id="{meeting}.A.words3" starttime="90.0" endtime="91.0">teletext</w>
</nite:root>""",
    )
    _write(
        root / "words" / f"{meeting}.B.words.xml",
        f"""<nite:root {NITE}>
<w nite:id="{meeting}.B.words0" starttime="75.0" endtime="76.0">yellow</w>
</nite:root>""",
    )
    _write(
        root / "dialogueActs" / f"{meeting}.A.dialog-act.xml",
        f"""<nite:root {NITE}>
<dact nite:id="{meeting}.A.dialog-act.x.2">
<nite:child href="{meeting}.A.words.xml#id({meeting}.A.words2)..id({meeting}.A.words2)"/>
</dact>
<dact nite:id="{meeting}.A.dialog-act.x.3">
<nite:child href="{meeting}.A.words.xml#id({meeting}.A.words3)"/>
</dact>
</nite:root>""",
    )
    _write(
        root / "dialogueActs" / f"{meeting}.B.dialog-act.xml",
        f"""<nite:root {NITE}>
<dact nite:id="{meeting}.B.dialog-act.x.1">
<nite:child href="{meeting}.B.words.xml#id({meeting}.B.words0)..id({meeting}.B.words0)"/>
</dact>
</nite:root>""",
    )


def _build_icsi(root: Path, meeting: str = "Bxx999") -> None:
    summarization = root / "ICSIplus" / "Contributions" / "Summarization"
    _write(summarization / "abstractive" / f"{meeting}.abssumm.xml", _abssumm(meeting))
    _write(
        summarization / "extractive" / f"{meeting}.summlink.xml",
        f"""<nite:root {NITE}>
<summlink nite:id="l1">
<nite:pointer role="extractive" href="{meeting}.B.dialogue-acts.xml#id({meeting}.B.dialogueact6)"/>
<nite:pointer role="abstractive" href="{meeting}.abssumm.xml#id({meeting}.s.3)"/>
</summlink>
</nite:root>""",
    )
    _write(
        root / "ICSIplus" / "DialogueActs" / f"{meeting}.B.dialogue-acts.xml",
        f"""<nite:root {NITE}>
<dialogueact nite:id="{meeting}.B.dialogueact6" starttime="43.151" endtime="45.0" type="z"/>
</nite:root>""",
    )


class TestAmiLayout:
    def test_every_scored_sentence_comes_back_with_its_layer_and_whitespace_normalised(
        self, tmp_path: Path
    ) -> None:
        _build_ami(tmp_path)
        items = read_gold_items(ami_layout(tmp_path), "ES9999a")
        scored = [i for i in items if i.layer in SCORED_LAYERS]
        assert [(i.layer, i.sentence_id) for i in scored] == [
            ("actions", "ES9999a.s.2"),
            ("decisions", "ES9999a.s.3"),
            ("decisions", "ES9999a.s.4"),
            ("decisions", "ES9999a.s.5"),
        ]
        assert scored[0].text == "Alice will send the budget."
        assert {i.layer for i in items} == {"abstract", "actions", "decisions", "problems"}

    def test_time_is_the_earliest_linked_act_across_channels(self, tmp_path: Path) -> None:
        _build_ami(tmp_path)
        by_id = {i.sentence_id: i for i in read_gold_items(ami_layout(tmp_path), "ES9999a")}
        yellow = by_id["ES9999a.s.3"]
        assert yellow.link_count == 2
        assert yellow.start_time == 60.25  # channel A's act, not channel B's 75.0

    def test_a_single_word_pointer_without_a_range_still_resolves(self, tmp_path: Path) -> None:
        _build_ami(tmp_path)
        by_id = {i.sentence_id: i for i in read_gold_items(ami_layout(tmp_path), "ES9999a")}
        assert by_id["ES9999a.s.4"].start_time == 90.0

    def test_unlinked_and_unresolvable_items_are_kept_not_dropped(self, tmp_path: Path) -> None:
        _build_ami(tmp_path)
        by_id = {i.sentence_id: i for i in read_gold_items(ami_layout(tmp_path), "ES9999a")}
        unlinked = by_id["ES9999a.s.5"]
        assert (unlinked.link_count, unlinked.start_time, unlinked.located) == (0, None, False)
        # s.2 has a link, but its dialogue act id does not exist: linked yet not locatable.
        dangling = by_id["ES9999a.s.2"]
        assert (dangling.link_count, dangling.start_time) == (1, None)

    def test_a_missing_meeting_raises(self, tmp_path: Path) -> None:
        _build_ami(tmp_path)
        with pytest.raises(FileNotFoundError):
            read_gold_items(ami_layout(tmp_path), "ES0000z")


class TestStripMarkers:
    def test_removes_every_inline_marker_and_collapses_the_gap(self) -> None:
        assert strip_markers("Yeah {vocalsound} so {disfmarker} we  {pause} go") == "Yeah so we go"

    def test_leaves_unmarked_text_alone_and_empties_a_marker_only_turn(self) -> None:
        assert strip_markers("Nice .") == "Nice ."
        assert strip_markers("{vocalsound}") == ""


class TestIcsiLayout:
    def test_time_comes_from_the_dialogue_act_starttime_attribute(self, tmp_path: Path) -> None:
        _build_icsi(tmp_path)
        layout = icsi_layout(tmp_path)
        by_id = {i.sentence_id: i for i in read_gold_items(layout, "Bxx999")}
        assert by_id["Bxx999.s.3"].start_time == pytest.approx(43.151)

    def test_icsi_declares_no_action_layer(self, tmp_path: Path) -> None:
        assert icsi_layout(tmp_path).has_actions is False
        assert ami_layout(tmp_path).has_actions is True


@pytest.mark.skipif(not NAS.is_dir(), reason="benchmark corpora not mounted at " + str(NAS))
class TestRealCorpusCounts:
    """The counts quoted in docs/design/summary_quality_eval_plan.md section 3.2."""

    @staticmethod
    def _tally(layout: CorpusLayout, meetings: list[str], layer: str) -> tuple[int, int, int]:
        items = [i for m in meetings for i in read_gold_items(layout, m) if i.layer == layer]
        return len(items), sum(1 for i in items if i.link_count), sum(i.located for i in items)

    def test_es2008_has_33_decisions_32_linked_and_17_actions_16_linked(self) -> None:
        layout = ami_layout(NAS / "ami" / "ami_public_manual_1.6.2")
        meetings = ["ES2008a", "ES2008b", "ES2008c", "ES2008d"]
        assert self._tally(layout, meetings, "decisions") == (33, 32, 32)
        assert self._tally(layout, meetings, "actions") == (17, 16, 16)

    @pytest.mark.parametrize(
        ("meeting", "expected"), [("Bed003", (8, 7, 7)), ("Bmr023", (7, 7, 7))]
    )
    def test_icsi_gate_meetings_decision_counts(self, meeting: str, expected: tuple) -> None:
        layout = icsi_layout(NAS / "icsi" / "ICSI_plus_NXT")
        assert self._tally(layout, [meeting], "decisions") == expected
        assert not [i for i in read_gold_items(layout, meeting) if i.layer == "actions"]
