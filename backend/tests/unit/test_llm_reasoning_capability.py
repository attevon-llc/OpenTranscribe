"""The reasoning off-switch is a MEASUREMENT, not a parameter the provider accepts (#64).

The numbers these tests are calibrated against were measured against a real vLLM
serving ``gemma-4-e4b`` at temperature 0, summing both response spellings:

===========================  =========  =======  ======
arm                          reasoning  content  tokens
===========================  =========  =======  ======
``enable_thinking: true``         1656      843    1123
``enable_thinking: false``         931      378     562
kwarg omitted (the control)        931      378     562
===========================  =========  =======  ======

``false`` is byte-identical to the control. vLLM answered 200 to all three, so
**accepting the parameter proved nothing**; only the comparison did. Every
verdict test below pairs a must-fire case with a control that must reach the
opposite conclusion, because a rule that always says "no off-switch" would pass
the gemma case and be useless.
"""

from __future__ import annotations

import json

import pytest

from app.core.constants import LLM_REASONING_CAPABILITY_KEY_PREFIX
from app.core.constants import LLM_REASONING_PROBE_MIN_CHARS
from app.core.constants import LLM_REASONING_PROBE_TEMPERATURE
from app.core.enums import ReasoningOffSwitch
from app.services import llm_reasoning
from app.services.llm_service import LLMConfig
from app.services.llm_service import LLMProvider
from app.services.llm_service import LLMService

#: The measured gemma-4-e4b arms, as (on, off, omitted) reasoning characters.
GEMMA_ARMS = (1656, 931, 931)

VLLM_BASE_URL = "http://llm-test-vllm:8000/v1"


def _config(provider: LLMProvider = LLMProvider.VLLM, model: str = "gemma-4-e4b") -> LLMConfig:
    return LLMConfig(
        provider=provider,
        model=model,
        base_url=VLLM_BASE_URL,
        api_key="not-a-secret",
    )


# --------------------------------------------------------------------------- #
# The verdict rule
# --------------------------------------------------------------------------- #


def test_the_measured_gemma_arms_report_no_off_switch():
    """The must-fire case: `false` == omitted means there is no switch.

    If this ever reports WORKS, the chat UI offers a toggle over a model that
    reasons anyway — a control whose label is a lie.
    """
    verdict, detail = llm_reasoning.verdict_from_arms(*GEMMA_ARMS)

    assert verdict is ReasoningOffSwitch.ABSENT
    assert "931" in detail, f"the detail must carry the evidence, got {detail!r}"


def test_a_model_that_actually_suppresses_reasoning_reports_a_working_switch():
    """The control. Without it, a rule that always answers ABSENT would pass above."""
    verdict, _ = llm_reasoning.verdict_from_arms(on=1656, off=0, omitted=900)

    assert verdict is ReasoningOffSwitch.WORKS


def test_halving_the_reasoning_is_not_an_off_switch():
    """A switch that leaves half the thinking in place still makes "off" a false word."""
    verdict, _ = llm_reasoning.verdict_from_arms(on=1600, off=500, omitted=1000)

    assert verdict is ReasoningOffSwitch.ABSENT


def test_a_quiet_off_arm_beside_an_equally_quiet_control_is_not_a_switch():
    """Condition 1: `off` must beat the OMITTED arm, not merely be small.

    A run where the model happened to reason briefly with no parameter at all
    proves nothing about the parameter.
    """
    verdict, _ = llm_reasoning.verdict_from_arms(on=1656, off=50, omitted=60)

    assert verdict is ReasoningOffSwitch.ABSENT


def test_a_quiet_off_arm_beside_a_quiet_activated_arm_is_not_a_switch():
    """Condition 2: `off` must also beat the ACTIVATED arm.

    Without it, a model that barely reasons when asked would score a working
    switch off a large, noisy control run.
    """
    verdict, _ = llm_reasoning.verdict_from_arms(on=100, off=90, omitted=1000)

    assert verdict is ReasoningOffSwitch.ABSENT


def test_a_model_that_never_reports_reasoning_has_nothing_to_switch_off():
    """Not ABSENT and not WORKS: there is no reasoning, so there is no control."""
    verdict, _ = llm_reasoning.verdict_from_arms(on=0, off=0, omitted=0)

    assert verdict is ReasoningOffSwitch.NO_REASONING


def test_a_stray_boundary_token_is_not_a_chain_of_thought():
    """Below the floor, a ratio is noise — so it is NO_REASONING, not WORKS."""
    below = LLM_REASONING_PROBE_MIN_CHARS - 1
    verdict, _ = llm_reasoning.verdict_from_arms(on=below, off=0, omitted=below)

    assert verdict is ReasoningOffSwitch.NO_REASONING


def test_only_works_may_render_a_control():
    """One place decides, and every other verdict must render nothing."""
    rendering = {
        verdict: llm_reasoning.ReasoningProbeResult(off_switch=verdict).control_renders
        for verdict in ReasoningOffSwitch
    }

    assert rendering == {
        ReasoningOffSwitch.UNKNOWN: False,
        ReasoningOffSwitch.UNSUPPORTED: False,
        ReasoningOffSwitch.NO_REASONING: False,
        ReasoningOffSwitch.ABSENT: False,
        ReasoningOffSwitch.WORKS: True,
    }


# --------------------------------------------------------------------------- #
# Reading the response: BOTH spellings, or the measurement reads as zero
# --------------------------------------------------------------------------- #


def test_reasoning_on_the_vllm_spelling_is_counted():
    """vLLM 0.19 reports `reasoning`. An earlier probe read only the other name."""
    assert llm_reasoning.reasoning_chars({"reasoning": "x" * 931}) == 931


def test_reasoning_on_the_parser_convention_spelling_is_counted():
    assert llm_reasoning.reasoning_chars({"reasoning_content": "y" * 400}) == 400


def test_both_spellings_are_summed():
    """A response carrying both must not be counted once."""
    message = {"reasoning": "a" * 10, "reasoning_content": "b" * 5}

    assert llm_reasoning.reasoning_chars(message) == 15


def test_an_answer_with_no_separated_reasoning_counts_zero():
    """The control: content alone is not reasoning, however long it is."""
    assert llm_reasoning.reasoning_chars({"content": "z" * 5000}) == 0


# --------------------------------------------------------------------------- #
# The fingerprint: what invalidates a recorded verdict
# --------------------------------------------------------------------------- #


def test_changing_the_model_changes_the_key():
    """The verdict belongs to the model, so a model swap must not inherit it."""
    before = llm_reasoning.capability_key("vllm", VLLM_BASE_URL, "gemma-4-e4b")
    after = llm_reasoning.capability_key("vllm", VLLM_BASE_URL, "qwen3-8b")

    assert before != after


def test_changing_the_endpoint_changes_the_key():
    """Two servers can run the same model name and behave differently."""
    before = llm_reasoning.capability_key("vllm", VLLM_BASE_URL, "gemma-4-e4b")
    after = llm_reasoning.capability_key("vllm", "http://elsewhere:8000/v1", "gemma-4-e4b")

    assert before != after


def test_a_trailing_slash_is_the_same_endpoint():
    """Otherwise one user's cosmetic edit re-probes a model already measured."""
    plain = llm_reasoning.capability_key("vllm", VLLM_BASE_URL, "gemma-4-e4b")
    slashed = llm_reasoning.capability_key("vllm", VLLM_BASE_URL + "/", "gemma-4-e4b")

    assert plain == slashed


def test_the_key_does_not_contain_the_endpoint():
    """The deployment-wide settings table must not become a directory of endpoints."""
    key = llm_reasoning.capability_key("vllm", "http://secret-host.internal:8000/v1", "m")

    assert "secret-host" not in key
    assert key.startswith(LLM_REASONING_CAPABILITY_KEY_PREFIX)


# --------------------------------------------------------------------------- #
# Recording and reading the verdict
# --------------------------------------------------------------------------- #


def test_an_unprobed_model_reports_unknown(db_session):
    """The default everywhere, and it renders no control."""
    verdict = llm_reasoning.read(db_session, "vllm", VLLM_BASE_URL, "never-probed-model")

    assert verdict is ReasoningOffSwitch.UNKNOWN


def test_a_recorded_verdict_is_read_back(db_session):
    """The control for the test above: recording must actually change the answer."""
    config = _config(model="records-round-trip")
    result = llm_reasoning.ReasoningProbeResult(
        off_switch=ReasoningOffSwitch.WORKS,
        reasoning_chars_on=1656,
        reasoning_chars_off=0,
        reasoning_chars_omitted=900,
        detail="measured",
    )
    llm_reasoning.record(db_session, config, result)

    assert (
        llm_reasoning.read(db_session, "vllm", VLLM_BASE_URL, "records-round-trip")
        is ReasoningOffSwitch.WORKS
    )


def test_the_record_keeps_the_evidence_not_just_the_verdict(db_session):
    """An operator asking "why is there no toggle" needs the numbers."""
    config = _config(model="keeps-evidence")
    llm_reasoning.record(
        db_session,
        config,
        llm_reasoning.ReasoningProbeResult(
            off_switch=ReasoningOffSwitch.ABSENT,
            reasoning_chars_on=1656,
            reasoning_chars_off=931,
            reasoning_chars_omitted=931,
            detail="off did not suppress reasoning",
        ),
    )

    stored = llm_reasoning.read_record(db_session, "vllm", VLLM_BASE_URL, "keeps-evidence")

    assert stored["reasoning_chars"] == {"on": 1656, "off": 931, "omitted": 931}
    assert stored["probed_at"], "a verdict with no timestamp cannot be aged out"


def test_recording_a_new_verdict_replaces_the_old_one(db_session):
    """A re-probe after a server upgrade must not leave the previous answer."""
    config = _config(model="replaces-verdict")
    llm_reasoning.record(
        db_session, config, llm_reasoning.ReasoningProbeResult(ReasoningOffSwitch.ABSENT)
    )
    llm_reasoning.record(
        db_session, config, llm_reasoning.ReasoningProbeResult(ReasoningOffSwitch.WORKS)
    )

    assert (
        llm_reasoning.read(db_session, "vllm", VLLM_BASE_URL, "replaces-verdict")
        is ReasoningOffSwitch.WORKS
    )


def test_a_verdict_this_build_does_not_know_reads_as_unknown():
    """A downgrade must lose the toggle, not the chat page."""
    assert llm_reasoning.verdict_of({"off_switch": "quantum"}) is ReasoningOffSwitch.UNKNOWN


def test_a_record_with_no_verdict_reads_as_unknown():
    assert llm_reasoning.verdict_of({}) is ReasoningOffSwitch.UNKNOWN


# --------------------------------------------------------------------------- #
# The probe itself
# --------------------------------------------------------------------------- #


class _RecordingTransport:
    """Captures every payload `_send_llm_request` is handed, and answers canned arms."""

    def __init__(self, reasoning_by_arm: dict[bool | None, int]):
        self.payloads: list[dict] = []
        self._by_arm = reasoning_by_arm

    # Bound as a plain class attribute, so `self._send_llm_request(...)` finds a
    # non-descriptor and passes no `self` — four arguments, not five.
    def __call__(self, _url, payload, _headers, _timeout):
        self.payloads.append(payload)
        kwargs = payload.get("chat_template_kwargs")
        arm = kwargs.get("enable_thinking") if isinstance(kwargs, dict) else None
        return {"choices": [{"message": {"content": "42", "reasoning": "r" * self._by_arm[arm]}}]}


@pytest.fixture
def recorded_probe(monkeypatch):
    """Drive the real probe against canned responses, capturing the requests."""

    def _install(reasoning_by_arm: dict[bool | None, int]) -> _RecordingTransport:
        transport = _RecordingTransport(reasoning_by_arm)
        monkeypatch.setattr(LLMService, "_send_llm_request", transport)
        return transport

    return _install


def test_the_probe_sends_three_genuinely_different_requests(recorded_probe):
    """`false` and "omitted" are different requests — measuring one as the other
    is exactly the mistake that made an off-switch look real."""
    transport = recorded_probe({True: 1656, False: 931, None: 931})

    llm_reasoning.probe(_config())

    arms = [p.get("chat_template_kwargs") for p in transport.payloads]
    assert arms == [{"enable_thinking": True}, {"enable_thinking": False}, None]


def test_the_probe_runs_at_temperature_zero(recorded_probe):
    """Sampling must not be available as an explanation for a difference."""
    transport = recorded_probe({True: 1656, False: 931, None: 931})

    llm_reasoning.probe(_config())

    temperatures = {p.get("temperature") for p in transport.payloads}
    assert temperatures == {LLM_REASONING_PROBE_TEMPERATURE}


def test_the_probe_reproduces_the_measured_gemma_verdict(recorded_probe):
    """End to end over the real code path, on the real numbers."""
    recorded_probe({True: 1656, False: 931, None: 931})

    result = llm_reasoning.probe(_config())

    assert result.off_switch is ReasoningOffSwitch.ABSENT
    assert result.control_renders is False


def test_the_probe_finds_a_real_off_switch(recorded_probe):
    """The control: the same code path must be able to reach WORKS."""
    recorded_probe({True: 1656, False: 0, None: 900})

    result = llm_reasoning.probe(_config())

    assert result.off_switch is ReasoningOffSwitch.WORKS
    assert result.control_renders is True


def test_a_provider_with_no_known_off_switch_is_never_dialled(monkeypatch):
    """`chat_template_kwargs` is a vLLM extension and a "custom" clone 400s on it.

    A probe that dialled one blindly would break chat for the deployments least
    able to debug it — the same reason those providers are excluded from
    ``llm_stream.USAGE_OPTION_PROVIDERS``.
    """

    def _explode(*_args, **_kwargs):
        raise AssertionError("the probe must not dial a provider it cannot probe")

    monkeypatch.setattr(LLMService, "_send_llm_request", _explode)

    result = llm_reasoning.probe(_config(provider=LLMProvider.CUSTOM))

    assert result.off_switch is ReasoningOffSwitch.UNSUPPORTED


def test_a_failing_endpoint_yields_unknown_rather_than_raising(monkeypatch):
    """A capability probe must degrade to "no control", never break the request."""

    def _fail(*_args, **_kwargs):
        raise ConnectionError("vLLM is down")

    monkeypatch.setattr(LLMService, "_send_llm_request", _fail)

    result = llm_reasoning.probe(_config())

    assert result.off_switch is ReasoningOffSwitch.UNKNOWN
    assert result.control_renders is False


# --------------------------------------------------------------------------- #
# The gate on the chat path — issue #439 must not regress
# --------------------------------------------------------------------------- #


def test_a_reasoning_off_preference_is_ignored_on_an_unprobed_model(db_session):
    """THE #439 non-regression.

    Returning None here is what makes ``_prepare_payload`` build the request
    exactly as it does today, i.e. still sending ``enable_thinking: true`` for
    vLLM. Returning False instead would ship the user a switch nobody measured.
    """
    service = LLMService(_config(model="unprobed-model"))

    assert llm_reasoning.resolve_enable_thinking(db_session, service, False) is None


def test_a_reasoning_off_preference_is_ignored_when_the_probe_said_absent(db_session):
    """The gemma case: the preference is stored, and deliberately not applied."""
    config = _config(model="absent-switch-model")
    llm_reasoning.record(
        db_session, config, llm_reasoning.ReasoningProbeResult(ReasoningOffSwitch.ABSENT)
    )
    service = LLMService(config)

    assert llm_reasoning.resolve_enable_thinking(db_session, service, False) is None


def test_a_reasoning_off_preference_is_honoured_when_the_probe_said_works(db_session):
    """The control. Without it, "always return None" passes both tests above."""
    config = _config(model="working-switch-model")
    llm_reasoning.record(
        db_session, config, llm_reasoning.ReasoningProbeResult(ReasoningOffSwitch.WORKS)
    )
    service = LLMService(config)

    assert llm_reasoning.resolve_enable_thinking(db_session, service, False) is False


@pytest.mark.parametrize("preference", [None, True])
def test_no_preference_never_touches_the_request(db_session, preference):
    """Only an explicit `False` may narrow anything."""
    config = _config(model="working-switch-model-2")
    llm_reasoning.record(
        db_session, config, llm_reasoning.ReasoningProbeResult(ReasoningOffSwitch.WORKS)
    )
    service = LLMService(config)

    assert llm_reasoning.resolve_enable_thinking(db_session, service, preference) is None


def test_the_recorded_verdict_is_not_an_editable_setting(db_session):
    """It is a measurement stored in SystemSettings, and it says so.

    An operator who hand-edits the row is asserting a capability nobody
    measured — the exact failure this feature exists to prevent — so the
    description has to warn them.
    """
    from app.models.system_settings import SystemSettings

    config = _config(model="describes-itself")
    llm_reasoning.record(
        db_session, config, llm_reasoning.ReasoningProbeResult(ReasoningOffSwitch.WORKS)
    )
    key = llm_reasoning.capability_key("vllm", VLLM_BASE_URL, "describes-itself")

    row = db_session.query(SystemSettings).filter(SystemSettings.key == key).one()

    assert "not an editable setting" in str(row.description)
    assert json.loads(str(row.value))["off_switch"] == "works"
