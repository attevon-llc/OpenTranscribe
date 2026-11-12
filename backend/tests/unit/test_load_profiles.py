"""The load test's named profiles (``scripts/perf/load/profiles.py``) and the shape that runs them.

Each profile is stepped through second by second, as Locust's shape runner would call it, and
checked against what the runbook promises: smoke is a minute, ramp holds 100/250/500/1000,
spike goes 0 to N to 0, soak lasts one to two hours, and pipeline runs only the uploader.
"""

from __future__ import annotations

import types

import pytest

from tests.fixtures import locust_stub as stub


@pytest.fixture
def load(monkeypatch):
    return stub.load_modules(monkeypatch)


def _timeline(profile, params, step: float = 1.0, limit: float = 4 * 3600.0):
    """``[(t, users)]`` at every ``step`` until the profile stops."""
    out: list[tuple[float, int]] = []
    t = 0.0
    while t < limit:
        tick = profile.tick(t, params)
        if tick is None:
            return out, t
        out.append((t, tick[0]))
        t += step
    raise AssertionError(f"{profile.name} never stopped")


def test_every_profile_loads_and_names_only_real_personas(load):
    profiles = load["profiles"]
    classes = load["locustfile"].USER_CLASSES

    assert set(profiles.PROFILES) == {
        "smoke",
        "steady",
        "ramp",
        "spike",
        "soak",
        "pipeline",
        "knee",
        "waves",
        "herd",
        "hot-file",
        "egress",
        "leak",
        "soak-8h",
        "soak-24h",
    }
    for profile in profiles.PROFILES.values():
        _timeline(profile, profiles.Params(), step=600.0, limit=30 * 3600.0)
        assert set(profile.personas) <= set(classes), profile.name
    assert "PipelineUploader" not in profiles.WEB_PERSONAS
    role_personas = (
        set(profiles.PEOPLE_PERSONAS)
        | set(profiles.HERD_PERSONAS)
        | set(profiles.HOT_FILE_PERSONAS)
    )
    assert set(profiles.WEB_PERSONAS) | {"PipelineUploader"} | role_personas == set(classes)


def test_smoke_is_a_few_people_for_a_minute(load):
    profiles = load["profiles"]
    timeline, stopped_at = _timeline(profiles.get("smoke"), profiles.Params())

    assert stopped_at == 60.0
    assert {users for _, users in timeline} == {10}


def test_steady_holds_the_target_after_the_ramp(load):
    profiles = load["profiles"]
    params = profiles.Params(users=300, rate=3.0, hold=600.0)
    _, stopped_at = _timeline(profiles.get("steady"), params)

    assert stopped_at == 100.0 + 600.0


def test_ramp_steps_up_and_holds_each_step(load):
    profiles = load["profiles"]
    params = profiles.Params(rate=10.0, hold=300.0)
    timeline, _ = _timeline(profiles.get("ramp"), params, step=10.0)
    steps = [
        users for i, (_, users) in enumerate(timeline) if i == 0 or users != timeline[i - 1][1]
    ]
    held = {u: sum(10.0 for _, users in timeline if users == u) for u in steps}

    assert steps == [100, 250, 500, 1000]
    assert all(seconds >= 300.0 for seconds in held.values()), held


def test_spike_goes_from_nobody_to_everyone_and_back(load):
    profiles = load["profiles"]
    timeline, _ = _timeline(profiles.get("spike"), profiles.Params(users=800))

    assert timeline[0][1] == 800  # no warm-up: the first tick asks for everyone
    assert timeline[-1][1] == 0
    assert [u for _, u in timeline].index(0) > 0


def test_soak_runs_one_to_two_hours(load):
    profiles = load["profiles"]
    _, stopped_at = _timeline(profiles.get("soak"), profiles.Params(), step=60.0)

    assert 3600.0 <= stopped_at <= 7200.0


def test_pipeline_runs_only_the_uploader_and_no_chat_users(load):
    profile = load["profiles"].get("pipeline")

    assert profile.personas == ("PipelineUploader",)
    assert profile.chat_users == 0


def test_an_unknown_profile_is_refused(load):
    with pytest.raises(KeyError, match="choose one of"):
        load["profiles"].get("burst")


def test_steps_and_numbers_come_from_the_command_line(load):
    options = stub.options(target_users=42, ramp_rate=1.5, hold=10.0, steps="5,10")

    params = load["profiles"].Params.from_options(options)

    assert params == load["profiles"].Params(users=42, rate=1.5, hold=10.0, steps=(5, 10))


def test_the_shape_hands_locust_the_chosen_profiles_classes(load):
    locustfile = load["locustfile"]
    shape = locustfile.ProfileShape()
    shape.runner = types.SimpleNamespace(
        environment=types.SimpleNamespace(parsed_options=stub.options(load_profile="pipeline"))
    )

    users, rate, classes = shape.tick()
    shape.run_time = 10 * 3600.0

    assert (users, rate) == (4, 1.0)
    assert classes == [locustfile.USER_CLASSES["PipelineUploader"]]
    assert shape.tick() is None


# --- the shapes of issue #1174 -------------------------------------------------------------


def _levels(timeline):
    """The user counts in order, consecutive repeats collapsed."""
    out: list[int] = []
    for _, users in timeline:
        if not out or out[-1] != users:
            out.append(users)
    return out


def test_knee_climbs_in_small_even_steps_and_holds_each(load):
    profiles = load["profiles"]
    timeline, _ = _timeline(profiles.get("knee"), profiles.Params(hold=60.0, rate=50.0), step=1.0)

    assert _levels(timeline) == [10, 25, 50, 100, 150, 200, 300, 400, 500]
    assert profiles.get("knee").personas == profiles.PEOPLE_PERSONAS


def test_waves_rise_dip_at_lunch_peak_again_and_fall_over_one_day(load):
    profiles = load["profiles"]
    params = profiles.Params(users=100, hold=1000.0)
    timeline, stopped_at = _timeline(profiles.get("waves"), params, step=1.0)
    levels = _levels(timeline)

    assert stopped_at == 1000.0
    assert max(levels) == 100
    peaks = [i for i in range(1, len(levels) - 1) if levels[i - 1] < levels[i] > levels[i + 1]]
    assert len(peaks) == 2, levels  # a morning and an afternoon peak
    assert levels[peaks[0] + 1] < 100 and levels[-1] <= 10  # a lunch dip, an evening tail


def test_waves_length_is_the_hold_so_a_short_run_has_the_same_shape(load):
    profiles = load["profiles"]
    short, stopped = _timeline(profiles.get("waves"), profiles.Params(users=50, hold=120.0))

    assert stopped == 120.0
    assert _levels(short)[0] == 5 and max(_levels(short)) == 50


def test_herd_signs_everyone_in_within_seconds_then_empties(load):
    profiles = load["profiles"]
    herd = profiles.get("herd")
    first = herd.tick(0.0, profiles.Params(users=500))

    assert first == (500, 100.0, ("LoginHerd",))  # 500 users at 100 a second: five seconds
    timeline, _ = _timeline(herd, profiles.Params(users=500))
    assert timeline[-1][1] == 0 and herd.chat_users == 0


def test_hot_file_runs_only_the_hot_file_reader(load):
    profiles = load["profiles"]
    timeline, stopped_at = _timeline(profiles.get("hot-file"), profiles.Params(users=200))

    assert profiles.get("hot-file").personas == ("HotFileReader",)
    assert max(users for _, users in timeline) == 200
    assert stopped_at == 20.0 + 300.0  # the ramp at 10 a second, then five minutes


def test_egress_and_leak_use_the_realistic_mix(load):
    profiles = load["profiles"]

    for name, minutes in (("egress", 10), ("leak", 20)):
        profile = profiles.get(name)
        _, stopped_at = _timeline(profile, profiles.Params(users=100, rate=100.0))
        assert profile.personas == profiles.PEOPLE_PERSONAS
        assert stopped_at == 1.0 + minutes * 60.0


@pytest.mark.parametrize(("name", "hours"), [("soak-8h", 8), ("soak-24h", 24)])
def test_the_long_soaks_run_their_full_length_by_default(load, name, hours):
    profiles = load["profiles"]
    _, stopped_at = _timeline(
        profiles.get(name), profiles.Params(users=100, rate=100.0), step=600.0, limit=30 * 3600.0
    )

    assert hours * 3600.0 <= stopped_at <= hours * 3600.0 + 600.0


def test_a_long_soak_shortens_to_two_minutes_with_hold_for_a_wiring_check(load):
    profiles = load["profiles"]
    for name in ("soak-8h", "soak-24h"):
        timeline, stopped_at = _timeline(
            profiles.get(name), profiles.Params(users=10, rate=10.0, hold=120.0)
        )

        assert stopped_at == 1.0 + 120.0
        assert {users for _, users in timeline} == {10}


def test_run_sh_accepts_every_profile_name(load):
    text = (stub.LOAD_DIR / "run.sh").read_text()

    missing = [
        name
        for name in load["profiles"].PROFILES
        if f"|{name}" not in text and f"{name}|" not in text
    ]
    assert not missing, missing
