"""Stored demo conversations name their speakers instead of quoting ``SPEAKER_NN`` (#628).

The conversations were generated while the seeded transcripts had no speaker names, so the
answers say "SPEAKER_05 says ...". A label means a different person in each recording, so a
name is used only when every recording the message cites agrees on it; otherwise the generic
label the app shows for an unnamed speaker is used.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.scripts.demo_seed import relabel
from app.scripts.demo_seed.__main__ import main

NAMES = {
    "bls-a": {"SPEAKER_00": "BLS narrator"},
    "bls-b": {"SPEAKER_00": "BLS narrator"},
    "nws-a": {"SPEAKER_00": "Forecaster"},
}


def _conv(content: str, cited: list[str], speaker: str = "SPEAKER_00") -> dict:
    return {
        "title": "t",
        "items": sorted({*cited, "bls-a"}),
        "messages": [
            {"role": "user", "content": "q"},
            {
                "role": "assistant",
                "content": content,
                "provider": "vllm",
                "model": "m",
                "citations": [{"item_id": i, "speaker": speaker, "snippet": "s"} for i in cited],
            },
        ],
    }


def test_a_label_the_cited_recordings_agree_on_becomes_that_name():
    out = relabel.relabel_conversation(
        _conv("SPEAKER_00 says **SPEAKER_00** again.", ["bls-a", "bls-b"]), NAMES
    )

    assert out["messages"][1]["content"] == "BLS narrator says **BLS narrator** again."


def test_a_label_the_cited_recordings_disagree_on_becomes_the_generic_label():
    out = relabel.relabel_conversation(_conv("SPEAKER_00 says hi.", ["bls-a", "nws-a"]), NAMES)

    assert out["messages"][1]["content"] == "Speaker 00 says hi."


def test_a_label_no_recording_names_becomes_the_generic_label():
    out = relabel.relabel_conversation(_conv("SPEAKER_07 spoke.", ["bls-a"]), NAMES)

    assert out["messages"][1]["content"] == "Speaker 07 spoke."


def test_a_citation_speaker_is_named_by_its_own_recording():
    out = relabel.relabel_conversation(_conv("x", ["bls-a", "nws-a"]), NAMES)

    assert [c["speaker"] for c in out["messages"][1]["citations"]] == ["BLS narrator", "Forecaster"]


def test_a_message_without_citations_is_resolved_against_the_conversations_items():
    conv = _conv("SPEAKER_00 spoke.", [])
    conv["messages"][1]["citations"] = []

    out = relabel.relabel_conversation(conv, NAMES)

    assert out["messages"][1]["content"] == "BLS narrator spoke."


def test_other_text_and_the_input_are_left_alone():
    conv = _conv("A SPEAKER_000X and SPEAKER_ stay; MYSPEAKER_00 too.", ["bls-a"])
    before = json.dumps(conv)

    out = relabel.relabel_conversation(conv, NAMES)

    assert out["messages"][1]["content"] == "A SPEAKER_000X and SPEAKER_ stay; MYSPEAKER_00 too."
    assert json.dumps(conv) == before


def _bundle(root: Path, with_names: bool) -> None:
    (root / "transcripts").mkdir(parents=True)
    (root / "conversations").mkdir()
    speakers = {"SPEAKER_00": "BLS narrator"} if with_names else {}
    (root / "transcripts/bls-a.json").write_text(json.dumps({"segments": [], "speakers": speakers}))
    (root / "conversations/c.json").write_text(json.dumps(_conv("SPEAKER_00 says.", ["bls-a"])))
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "items": [{"id": "bls-a", "transcript": "transcripts/bls-a.json"}],
                "conversations": [{"id": "c", "file": "conversations/c.json"}],
            }
        )
    )


def test_the_command_writes_a_copy_and_never_touches_the_original(tmp_path: Path):
    src, out = tmp_path / "src", tmp_path / "out"
    _bundle(src, with_names=True)
    original = (src / "conversations/c.json").read_text()

    assert main(["relabel", "--bundle", str(src), "--out", str(out)]) == 0

    copy = json.loads((out / "conversations/c.json").read_text())
    assert copy["messages"][1]["content"] == "BLS narrator says."
    assert (src / "conversations/c.json").read_text() == original
    assert (out / "manifest.json").is_file()


def test_names_the_transcripts_lack_come_from_the_names_file(tmp_path: Path):
    src, out = tmp_path / "src", tmp_path / "out"
    _bundle(src, with_names=False)
    names = tmp_path / "n.json"
    names.write_text(json.dumps({"bls-a": {"SPEAKER_00": "Economist"}}))

    main(["relabel", "--bundle", str(src), "--out", str(out), "--names", str(names)])

    copy = json.loads((out / "conversations/c.json").read_text())
    assert copy["messages"][1]["content"] == "Economist says."


def test_the_command_refuses_to_write_over_an_existing_directory(tmp_path: Path):
    src = tmp_path / "src"
    _bundle(src, with_names=True)

    assert main(["relabel", "--bundle", str(src), "--out", str(tmp_path)]) == 2


@pytest.mark.parametrize("with_names", [True, False])
def test_no_raw_label_survives_either_way(tmp_path: Path, with_names: bool):
    src, out = tmp_path / "src", tmp_path / "out"
    _bundle(src, with_names=with_names)

    main(["relabel", "--bundle", str(src), "--out", str(out)])

    assert "SPEAKER_" not in (out / "conversations/c.json").read_text()
