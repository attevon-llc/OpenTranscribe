"""Hash-based offline password blocklist (SHA-1 of the NFKC password, Pwned Passwords format)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path

import pytest

from app.auth import password_blocklist
from app.auth.password_blocklist import blocklist_status
from app.auth.password_blocklist import is_blocklisted
from app.auth.password_blocklist import load_blocklist
from app.scripts import build_password_blocklist as builder

COMMON = ["password", "123456", "qwerty", "letmein"]


def _sha1(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8"), usedforsecurity=False).hexdigest().upper()  # noqa: S324


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """A hash list at the default location, in the shape the downloader writes."""
    path = tmp_path / "pwned-passwords-top100k-sha1.txt"
    path.write_text("".join(f"{_sha1(p)}\n" for p in COMMON))
    path.with_name(path.name + ".meta.json").write_text(json.dumps({"retrieved": "2026-10-01"}))
    monkeypatch.setattr(password_blocklist, "default_blocklist_path", lambda: path)
    return path


@pytest.fixture
def absent(tmp_path, monkeypatch):
    monkeypatch.setattr(password_blocklist, "default_blocklist_path", lambda: tmp_path / "none.txt")


class TestInstalledList:
    @pytest.mark.parametrize("password", COMMON)
    def test_known_common_passwords_are_rejected_via_hash(self, installed, password):
        assert is_blocklisted(password)

    def test_case_variant_is_caught_by_the_lowercase_check(self, installed):
        assert _sha1("PASSWORD") not in load_blocklist()
        assert is_blocklisted("PASSWORD")
        assert is_blocklisted("QwErTy")

    def test_full_width_spelling_is_normalised_first(self, installed):
        assert is_blocklisted("ｐａｓｓｗｏｒｄ")

    def test_strong_passphrase_is_accepted(self, installed):
        assert not is_blocklisted("marble tractor quietly up")
        assert not is_blocklisted("correct horse battery staple 7 violet")

    def test_file_holds_hashes_not_plaintext(self, installed):
        lines = installed.read_text().splitlines()
        assert lines
        assert all(re.fullmatch(r"[0-9A-F]{40}", line) for line in lines)

    def test_status(self, installed):
        status = blocklist_status()
        assert status == {
            "installed": True,
            "entries": 4,
            "source": "default",
            "retrieved": "2026-10-01",
        }


class TestAbsentList:
    def test_check_is_skipped_never_raises(self, absent):
        assert is_blocklisted("password") is False

    def test_status_says_not_installed(self, absent):
        status = blocklist_status()
        assert status["installed"] is False
        assert status["entries"] == 0

    def test_single_warning_names_the_install_command(self, absent, monkeypatch, caplog):
        monkeypatch.setattr(password_blocklist, "_missing_warned", False)
        with caplog.at_level(logging.WARNING):
            for _ in range(3):
                is_blocklisted("password")
            password_blocklist.log_startup_status(True)
        warnings = [r.getMessage() for r in caplog.records if "not installed" in r.getMessage()]
        assert len(warnings) == 1
        assert "./opentranscribe.sh download-models password-blocklist" in warnings[0]

    def test_startup_status_is_silent_when_the_check_is_off(self, absent, monkeypatch, caplog):
        monkeypatch.setattr(password_blocklist, "_missing_warned", False)
        with caplog.at_level(logging.WARNING):
            password_blocklist.log_startup_status(False)
        assert "not installed" not in caplog.text


class TestOperatorFile:
    def test_hash_file(self, tmp_path, installed):
        path = tmp_path / "hashes.txt"
        path.write_text(f"{_sha1('corp-secret-phrase')}\n{_sha1('Another-Phrase-9').lower()}\n\n")
        assert is_blocklisted("corp-secret-phrase", str(path))
        assert is_blocklisted("Another-Phrase-9", str(path))
        assert is_blocklisted("CORP-SECRET-PHRASE", str(path))  # via the lowercase form
        assert not is_blocklisted("password", str(path))  # replaces the default list

    def test_hash_file_with_counts(self, tmp_path):
        path = tmp_path / "hashes.txt"
        path.write_text(f"{_sha1('counted-phrase-1')}:12345\n")
        assert is_blocklisted("counted-phrase-1", str(path))

    def test_legacy_plaintext_file_stays_case_insensitive(self, tmp_path):
        path = tmp_path / "plain.txt"
        path.write_text("Legacy-Phrase-One\nsecond legacy phrase\n")
        assert is_blocklisted("legacy-phrase-one", str(path))
        assert is_blocklisted("LEGACY-PHRASE-ONE", str(path))
        assert is_blocklisted("second legacy phrase", str(path))
        assert not is_blocklisted("password", str(path))

    def test_mixed_file(self, tmp_path):
        path = tmp_path / "mixed.txt"
        path.write_text(f"{_sha1('hashed-entry-x')}\nplain-entry-x\n")
        assert is_blocklisted("hashed-entry-x", str(path))
        assert is_blocklisted("plain-entry-x", str(path))

    def test_edits_are_picked_up_by_mtime(self, tmp_path):
        path = tmp_path / "hashes.txt"
        path.write_text(f"{_sha1('first-phrase-1')}\n")
        assert is_blocklisted("first-phrase-1", str(path))
        path.write_text(f"{_sha1('second-phrase-2')}\n")
        os.utime(path, (1, path.stat().st_mtime + 5))
        assert is_blocklisted("second-phrase-2", str(path))
        assert not is_blocklisted("first-phrase-1", str(path))

    def test_unreadable_override_falls_back_to_default(self, installed):
        assert is_blocklisted("password", "/nonexistent/hashes.txt")


class TestBuilder:
    def test_parse_range_body_filters_padding_and_garbage(self):
        suffix_a = "A" * 35
        suffix_b = "B" * 35
        body = f"{suffix_a}:5000\n{suffix_b}:3\n{'C' * 35}:0\nnot-a-line\n{'d' * 35}:x\n"
        assert builder.parse_range_body("00000", body, min_count=1000) == [
            ("00000" + suffix_a, 5000)
        ]

    def test_select_top_orders_by_count_then_hash(self):
        entries = [("B" * 40, 10), ("A" * 40, 10), ("C" * 40, 99), ("D" * 40, 1)]
        assert [h[0] for h, _ in builder.select_top(entries, 3)] == ["C", "A", "B"]

    def test_download_is_resumable_and_selects(self, tmp_path):
        import asyncio

        import httpx

        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            prefix = request.url.path.rsplit("/", 1)[-1]
            calls.append(prefix)
            suffix = _sha1("123456")[5:] if prefix == _sha1("123456")[:5] else "0" * 35
            return httpx.Response(200, text=f"{suffix}:{9000 if prefix[0] != '0' else 1}\n")

        cache = tmp_path / "cache"
        asyncio.run(
            builder.download(
                cache,
                min_count=100,
                concurrency=2,
                shard_limit=2,
                transport=httpx.MockTransport(handler),
            )
        )
        assert len(calls) == 2 * 256
        asyncio.run(
            builder.download(
                cache,
                min_count=100,
                concurrency=2,
                shard_limit=2,
                transport=httpx.MockTransport(handler),
            )
        )
        assert len(calls) == 2 * 256  # finished shards are skipped
        assert len(list(Path(cache).glob("*.txt"))) == 2

    def test_main_refuses_to_write_an_incomplete_list(self, tmp_path, monkeypatch):
        async def fake_download(*_a, **_k):
            return None

        cache = tmp_path / "cache"
        cache.mkdir()
        for shard in builder.all_shards(1):
            (cache / f"{shard}.txt").write_text(f"{'A' * 40}:5000\n")
        monkeypatch.setattr(builder, "download", fake_download)
        out = tmp_path / "out.txt"
        code = builder.main(
            ["--cache-dir", str(cache), "--out", str(out), "--top", "5", "--shard-limit", "1"]
        )
        assert code == 1
        assert not out.exists()


def test_cache_is_not_shared_between_paths(tmp_path):
    first = tmp_path / "a.txt"
    second = tmp_path / "b.txt"
    first.write_text(f"{_sha1('only-in-a-1')}\n")
    second.write_text(f"{_sha1('only-in-b-1')}\n")
    assert password_blocklist.is_blocklisted("only-in-a-1", str(first))
    assert not password_blocklist.is_blocklisted("only-in-a-1", str(second))


def test_main_writes_list_and_metadata(tmp_path, monkeypatch):
    async def fake_download(*_a, **_k):
        return None

    cache = tmp_path / "cache"
    cache.mkdir()
    for shard in builder.all_shards(1):
        (cache / f"{shard}.txt").write_text(f"{'B' * 40}:5000\n{'A' * 40}:9000\n")
    monkeypatch.setattr(builder, "download", fake_download)
    out = tmp_path / "out" / "list.txt"
    code = builder.main(
        ["--cache-dir", str(cache), "--out", str(out), "--top", "2", "--shard-limit", "1"]
    )
    assert code == 0
    assert out.read_text().splitlines() == ["A" * 40, "B" * 40]
    meta = json.loads(out.with_name("list.txt.meta.json").read_text())
    assert meta["entries"] == 2
    assert meta["highest_count"] == 9000
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", meta["retrieved"])
