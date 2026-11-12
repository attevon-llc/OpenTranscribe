"""The demo seed bundle's format and licence policy (#628).

Only open-licence, non-personal content may reach the public demo. ``load_bundle`` is the
gate: a valid bundle loads, and each kind of defect is refused with a reason naming the
item. The committed catalogue of candidate sources is held to the same licence list.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.scripts.demo_seed.__main__ import main
from app.scripts.demo_seed.bundle import ALLOWED_LICENSES
from app.scripts.demo_seed.bundle import ATTRIBUTION_REQUIRED
from app.scripts.demo_seed.bundle import COLLECTIONS
from app.scripts.demo_seed.bundle import BundleError
from app.scripts.demo_seed.bundle import load_bundle

REPO_ROOT = Path(__file__).resolve().parents[3]
CATALOG = REPO_ROOT / "demo" / "seed-catalog.json"

MEDIA = b"ID3 not really audio, but bytes with a known digest"


def _item(**overrides):
    item = {
        "id": "nasa-briefing",
        "title": "A mission briefing",
        "language": "en",
        "collection": "science-briefings",
        "media": "media/nasa-briefing.mp3",
        "media_sha256": hashlib.sha256(MEDIA).hexdigest(),
        "transcript": "transcripts/nasa-briefing.json",
        "source_url": "https://www.nasa.gov/",
        "license": "US-GOV-PD",
        "license_url": "https://www.nasa.gov/nasa-brand-center/images-and-media/",
        "attribution": "NASA",
        "contains_private_individuals": False,
        "synthetic_pii": False,
    }
    item.update(overrides)
    return {k: v for k, v in item.items() if v is not None}


def _write(root: Path, items: list[dict], segments=None, **manifest) -> Path:
    (root / "media").mkdir(parents=True, exist_ok=True)
    (root / "transcripts").mkdir(exist_ok=True)
    (root / "media" / "nasa-briefing.mp3").write_bytes(MEDIA)
    (root / "transcripts" / "nasa-briefing.json").write_text(
        json.dumps(
            {
                "segments": segments
                if segments is not None
                else [
                    {"start": 0.0, "end": 2.5, "speaker": "SPEAKER_00", "text": "Good morning."},
                    {"start": 2.5, "end": 5.0, "speaker": "SPEAKER_01", "text": "Thank you."},
                ],
                "speakers": {"SPEAKER_00": "Flight director", "SPEAKER_01": "Reporter"},
            }
        )
    )
    body = {"schema": 1, "bundle_version": "2026.11.1", "items": items, **manifest}
    (root / "manifest.json").write_text(json.dumps(body))
    return root


def _problems(root: Path) -> list[str]:
    with pytest.raises(BundleError) as caught:
        load_bundle(root)
    return caught.value.problems


def test_a_valid_bundle_loads(tmp_path):
    version, items = load_bundle(_write(tmp_path, [_item()]))

    assert version == "2026.11.1"
    assert [i.id for i in items] == ["nasa-briefing"]
    assert [s.speaker for s in items[0].segments()] == ["SPEAKER_00", "SPEAKER_01"]
    assert items[0].speaker_names()["SPEAKER_00"] == "Flight director"


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"license": "CC-BY-NC-4.0"}, "licence 'CC-BY-NC-4.0' is not allowed"),
        ({"license": "All rights reserved"}, "is not allowed"),
        ({"contains_private_individuals": True}, "contains_private_individuals: false"),
        ({"contains_private_individuals": None}, "contains_private_individuals: false"),
        ({"attribution": None}, "missing attribution"),
        ({"license_url": None}, "missing license_url"),
        ({"media_sha256": "0" * 64}, "SHA-256 does not match"),
        ({"media": "../../etc/passwd"}, "outside the bundle"),
        ({"transcript": "transcripts/missing.json"}, "transcript missing"),
        ({"collection": "weather-memes"}, "unknown collection 'weather-memes'"),
        ({"collection": None}, "missing collection"),
        ({"synthetic_pii": "yes"}, "synthetic_pii must be true or false"),
    ],
)
def test_a_defective_item_is_refused_with_a_reason(tmp_path, overrides, expected):
    problems = _problems(_write(tmp_path, [_item(**overrides)]))

    assert any(expected in p and p.startswith("nasa-briefing:") for p in problems), problems


@pytest.mark.parametrize(
    ("segments", "expected"),
    [
        ([], "no segments"),
        ([{"start": 3.0, "end": 1.0, "speaker": "S", "text": "x"}], "start 3.0 >= end 1.0"),
        (
            [
                {"start": 5.0, "end": 6.0, "speaker": "S", "text": "a"},
                {"start": 1.0, "end": 2.0, "speaker": "S", "text": "b"},
            ],
            "starts before the previous one",
        ),
        ([{"start": 0.0, "end": 1.0, "speaker": "S", "text": "  "}], "empty speaker or text"),
        ([{"start": 0.0, "end": 1.0}], "lacks start/end/speaker/text"),
    ],
)
def test_a_malformed_transcript_is_refused(tmp_path, segments, expected):
    problems = _problems(_write(tmp_path, [_item()], segments=segments))

    assert any(expected in p for p in problems), problems


def test_manifest_level_defects_are_refused(tmp_path):
    problems = _problems(_write(tmp_path, [_item(), _item()], schema=2, bundle_version=""))

    assert "schema must be 1" in problems
    assert "missing bundle_version" in problems
    assert "nasa-briefing: duplicate id" in problems


def test_the_cli_validates_and_exits_non_zero_on_a_bad_bundle(tmp_path, capsys):
    good = _write(tmp_path / "good", [_item()])
    bad = _write(tmp_path / "bad", [_item(license="CC-BY-NC-4.0")])

    assert main(["validate", "--bundle", str(good)]) == 0
    assert main(["validate", "--bundle", str(bad)]) == 1
    assert "not allowed on the demo" in capsys.readouterr().err


def test_import_refuses_while_demo_mode_is_on(tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "DEMO_MODE", True)

    assert main(["import", "--bundle", str(_write(tmp_path, [_item()]))]) == 2


def test_every_catalogue_source_uses_an_allowed_licence():
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))

    assert catalog["sources"], "the catalogue lists no sources"
    for source in catalog["sources"]:
        assert source["license"] in ALLOWED_LICENSES, source["name"]
        assert source["license_url"].startswith("https://"), source["name"]
        assert source["contains_private_individuals"] is False, source["name"]


def test_every_catalogue_source_names_a_known_collection_with_its_reasons():
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))

    assert {c["id"]: c["name"] for c in catalog["collections"]} == COLLECTIONS
    assert {s["collection"] for s in catalog["sources"]} == set(COLLECTIONS)
    for source in catalog["sources"]:
        assert source["why_allowed"].strip(), source["name"]
        assert source["exclusions"], source["name"]


def test_the_reno_snowpack_video_is_catalogued_as_cc_by_with_its_credit_line():
    """Its own YouTube page declares CC BY, narrower than the NWS public-domain disclaimer."""
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))

    reno = [s for s in catalog["sources"] if "Reno snowpack" in s["name"]]

    assert len(reno) == 1
    assert reno[0]["license"] == "CC-BY-4.0"
    assert reno[0]["license"] in ATTRIBUTION_REQUIRED
    assert reno[0]["attribution"].strip()
