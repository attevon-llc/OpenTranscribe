"""A fixed slice of the demo seed bundle, as a bundle of its own (#43).

The visual baselines and docs screenshots photograph a few neutral, licensed bundle items
instead of the benchmark corpus. ``demo_seed subset`` selects them: only the named items are
validated (licence, provenance, media digest), the bundle version can be pinned so a rebuilt
bundle cannot silently change the photographed corpus, and ``--out`` writes a bundle that
``demo_seed import`` reads unchanged, carrying only the conversations wholly inside the slice.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.scripts.demo_seed.__main__ import main
from app.scripts.demo_seed.bundle import BundleError
from app.scripts.demo_seed.bundle import load_bundle
from app.scripts.demo_seed.conversations import load_conversations
from app.scripts.demo_seed.subset import write_subset
from tests.unit._demo_bundle import VERSION
from tests.unit._demo_bundle import item as _item
from tests.unit._demo_bundle import media_bytes as _media
from tests.unit._demo_bundle import write_bundle as _bundle


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    items = [_item("a"), _item("b", ext="mp3"), _item("c"), _item("d")]
    return _bundle(tmp_path / "src", items, {"ab": ["a", "b"], "cd": ["c", "d"], "ac": ["a", "c"]})


class TestOnlyTheSelectedItemsAreValidated:
    def test_the_selection_loads_in_the_order_asked(self, bundle: Path):
        version, items = load_bundle(bundle, only=["b", "a"])

        assert version == VERSION
        assert [i.id for i in items] == ["b", "a"]

    def test_a_defect_in_an_unselected_item_does_not_block_the_selection(self, bundle: Path):
        (bundle / "media" / "d.mp4").write_bytes(b"tampered")
        with pytest.raises(BundleError):
            load_bundle(bundle)

        _, items = load_bundle(bundle, only=["a"])

        assert [i.id for i in items] == ["a"]

    def test_a_defect_in_a_selected_item_is_refused(self, bundle: Path):
        (bundle / "media" / "a.mp4").write_bytes(b"tampered")

        with pytest.raises(BundleError) as caught:
            load_bundle(bundle, only=["a", "b"])

        assert caught.value.problems == ["a: media SHA-256 does not match the manifest"]

    def test_an_id_not_in_the_bundle_is_refused(self, bundle: Path):
        with pytest.raises(BundleError) as caught:
            load_bundle(bundle, only=["a", "zz"])

        assert "zz: not in the bundle" in caught.value.problems

    def test_an_id_asked_twice_is_refused(self, bundle: Path):
        with pytest.raises(BundleError) as caught:
            load_bundle(bundle, only=["a", "a"])

        assert "a: selected more than once" in caught.value.problems

    def test_an_empty_selection_is_refused(self, bundle: Path):
        with pytest.raises(BundleError) as caught:
            load_bundle(bundle, only=[])

        assert "no items selected" in caught.value.problems


class TestTheWrittenSubset:
    def test_it_is_a_bundle_holding_only_the_selection(self, bundle: Path, tmp_path: Path):
        out = tmp_path / "out"

        version, items = write_subset(bundle, out, ["a", "b", "c"])

        assert version == VERSION
        assert [i.id for i in items] == ["a", "b", "c"]
        assert [i.id for i in load_bundle(out)[1]] == ["a", "b", "c"]
        assert sorted(p.name for p in (out / "media").iterdir()) == ["a.mp4", "b.mp3", "c.mp4"]

    def test_only_conversations_wholly_inside_the_selection_are_kept(
        self, bundle: Path, tmp_path: Path
    ):
        out = tmp_path / "out"

        write_subset(bundle, out, ["a", "b", "c"])

        kept = load_conversations(out, {"a", "b", "c"})
        assert sorted(c.id for c in kept) == ["ab", "ac"]
        assert sorted(p.name for p in (out / "conversations").iterdir()) == ["ab.json", "ac.json"]

    def test_a_pinned_bundle_version_that_differs_is_refused(self, bundle: Path, tmp_path: Path):
        with pytest.raises(BundleError) as caught:
            write_subset(bundle, tmp_path / "out", ["a"], bundle_version="2026.12.1")

        assert caught.value.problems == [
            f"the bundle is version {VERSION}; the selection pins 2026.12.1"
        ]
        assert not (tmp_path / "out").exists()

    def test_a_non_empty_output_directory_is_refused(self, bundle: Path, tmp_path: Path):
        out = tmp_path / "out"
        out.mkdir()
        (out / "left-over").write_text("x", encoding="utf-8")

        with pytest.raises(BundleError) as caught:
            write_subset(bundle, out, ["a"])

        assert "is not empty" in caught.value.problems[0]


class TestTheCommand:
    def test_it_lists_each_selected_file_and_its_size(self, bundle: Path, capsys):
        assert main(["subset", "--bundle", str(bundle), "--items", "b,a"]) == 0

        out = capsys.readouterr().out.splitlines()
        assert out == [f"b.mp3\t{len(_media('b'))}", f"a.mp4\t{len(_media('a'))}"]

    def test_out_writes_the_bundle_and_lists_its_files(self, bundle: Path, tmp_path: Path, capsys):
        out = tmp_path / "out"

        rc = main(
            ["subset", "--bundle", str(bundle), "--items", "a", "--bundle-version", VERSION]
            + ["--out", str(out)]
        )

        assert rc == 0
        assert capsys.readouterr().out.splitlines() == [f"a.mp4\t{len(_media('a'))}"]
        assert (out / "manifest.json").is_file()

    def test_a_refused_selection_exits_one_naming_the_problem(self, bundle: Path, capsys):
        assert main(["subset", "--bundle", str(bundle), "--items", "a,zz"]) == 1

        assert "bundle: zz: not in the bundle" in capsys.readouterr().err

    def test_items_is_required(self, bundle: Path, capsys):
        assert main(["subset", "--bundle", str(bundle)]) == 2

        assert "subset needs --items" in capsys.readouterr().err
