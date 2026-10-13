"""sanitize_filename strips control and bidi characters from the whole name, extension included."""

import pytest

from app.utils.filename import sanitize_filename


@pytest.mark.parametrize("ctl", ["\r", "\n", "\x00", "\x1b", "\x7f", "\x85", "\x9f"])
def test_control_chars_removed_from_extension(ctl):
    out = sanitize_filename(f"talk.mp{ctl}3")
    assert out == "talk.mp3"


@pytest.mark.parametrize("ctl", ["\r", "\n", "\x00", "\x85"])
def test_control_chars_removed_from_name(ctl):
    assert sanitize_filename(f"ta{ctl}lk.mp3") == "talk.mp3"


def test_crlf_after_extension_cannot_survive():
    out = sanitize_filename("talk.mp3\r\nX-Injected: 1")
    assert "\r" not in out
    assert "\n" not in out


@pytest.mark.parametrize("bidi", ["‮", "‭", "‪", "‫", "‬", "⁦", "⁧", "⁨", "⁩", "‎", "‏"])
def test_bidi_overrides_removed(bidi):
    out = sanitize_filename(f"invoice{bidi}3pm.mp4")
    assert out == "invoice3pm.mp4"
    assert out.isascii()


def test_bidi_in_extension_removed():
    assert sanitize_filename("a.m‮p4") == "a.mp4"


def test_normal_behaviour_unchanged():
    assert sanitize_filename("My File—With Issues.mp3") == "My File-With Issues.mp3"
    assert sanitize_filename("") == "unnamed_file"
    assert sanitize_filename("\x00\x01") == "unnamed_file"
