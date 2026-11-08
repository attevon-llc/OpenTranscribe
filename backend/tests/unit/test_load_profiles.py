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

    assert set(profiles.PROFILES) == {"smoke", "steady", "ramp", "spike", "soak", "pipeline"}
    for profile in profiles.PROFILES.values():
        _timeline(profile, profiles.Params(), step=30.0)
        assert set(profile.personas) <= set(classes), profile.name
    assert "PipelineUploader" not in profiles.WEB_PERSONAS
    assert set(profiles.WEB_PERSONAS) | {"PipelineUploader"} == set(classes)


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
