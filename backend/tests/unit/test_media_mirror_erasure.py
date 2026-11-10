"""The erasure-only delete on the media mirror destination (issue #1353, R13).

The mirror engine never deletes; this is the one other path that touches the destination, and it
must remove only what the erasure names. A folder destination is real files here, so nothing is
mocked except the settings that select it.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.services import media_mirror_erasure as mirror


@pytest.fixture()
def folder(tmp_path):
    for key in (
        "user_7/file_1/original.mp4",
        "user_7/file_1/thumb.jpg",
        "user_7/file_2/original.mp4",
        "user_70/file_1/original.mp4",
        "user_8/file_1/original.mp4",
    ):
        path = tmp_path / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    cfg = {"enabled": True, "destination_type": "local", "destination": str(tmp_path)}
    with patch("app.services.media_mirror_service.get_settings", return_value=cfg):
        yield tmp_path


def test_a_named_prefix_is_deleted_and_counted_and_nothing_else(folder):
    assert mirror.count_mirrored(["user_7/file_1/"]) == 2
    assert mirror.erase_mirrored(["user_7/file_1/"]) == 2
    assert not (folder / "user_7/file_1").exists()
    for survivor in ("user_7/file_2/original.mp4", "user_8/file_1/original.mp4"):
        assert (folder / survivor).is_file()
    assert mirror.count_mirrored(["user_7/file_1/"]) == 0


def test_a_user_prefix_does_not_reach_a_longer_user_id(folder):
    assert mirror.erase_mirrored(["user_7/"]) == 3
    assert (folder / "user_70/file_1/original.mp4").is_file(), "user_7/ must not match user_70/"


@pytest.mark.parametrize("prefix", ["../x/", "/etc/", "user_7/../../x/"])
def test_an_unsafe_prefix_is_refused(folder, prefix):
    with pytest.raises(ValueError):
        mirror.erase_mirrored([prefix])


def test_a_deployment_without_a_mirror_has_nothing_to_count_or_delete():
    with patch(
        "app.services.media_mirror_service.get_settings",
        return_value={"enabled": False, "destination_type": "local", "destination": ""},
    ):
        assert mirror.count_mirrored(["user_7/"]) == 0
        assert mirror.erase_mirrored(["user_7/"]) == 0


def test_the_engine_still_has_no_delete_for_a_run():
    """The never-delete invariant of a mirror run is untouched by this module."""
    from app.services import media_mirror_engine as engine

    for cls in (engine.FolderDestination, engine.S3Destination):
        assert not [n for n in dir(cls) if "delete" in n.lower() or "remove" in n.lower()]


def test_a_residue_sweep_deletes_and_counts_only_what_it_selects(folder):
    """An account erasure that keeps a file's directory takes the rest of the prefix."""
    from app.services.erasure_receipt.types import ResidueSweep

    (folder / "user_7/youtube_abc").mkdir()
    (folder / "user_7/youtube_abc/thumbnail.jpg").write_bytes(b"x")
    sweep = ResidueSweep("user_7/", keep=("user_7/file_2/",), elsewhere=("user_7/file_1/",))

    assert mirror.count_mirrored_selected(sweep.prefix, sweep.selects) == 1
    assert mirror.erase_mirrored_selected(sweep.prefix, sweep.selects) == 1
    assert not (folder / "user_7/youtube_abc/thumbnail.jpg").exists()
    for survivor in (
        "user_7/file_1/original.mp4",
        "user_7/file_2/original.mp4",
        "user_70/file_1/original.mp4",
    ):
        assert (folder / survivor).is_file(), survivor
    assert mirror.count_mirrored_selected(sweep.prefix, sweep.selects) == 0
