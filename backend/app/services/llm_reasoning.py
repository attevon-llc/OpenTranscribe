"""Reasoning as a per-MODEL capability, measured rather than assumed (issue #64).

A provider returning HTTP 200 for a "do not reason" parameter is not evidence the
model honoured it. Measured against a real vLLM serving ``gemma-4-e4b`` at
temperature 0, summing **both** response spellings (``reasoning`` and
``reasoning_content``):

===========================  =========  =======  ======
arm                          reasoning  content  tokens
===========================  =========  =======  ======
``enable_thinking: true``         1656      843    1123
``enable_thinking: false``         931      378     562
kwarg omitted (the control)        931      378     562
===========================  =========  =======  ======

``false`` is byte-identical to the control: the chat template ignores the off
value and the model reasons anyway. Exposing a toggle over that model would tell
the user reasoning is off while 931 characters of it are still generated — worse
than offering no toggle, because it is a false claim rather than a missing
feature. ``true`` *does* change behaviour, which is why activation for vLLM
(issue #439) stays unconditional.

So the switch ships only where it demonstrably works, and "demonstrably" means
this module ran the three-arm comparison against that exact model and recorded
the verdict.

Three design points that are not obvious:

* **The probe is NON-streaming, deliberately.** vLLM's *streaming* reasoning
  parser only enters reasoning mode on the template's opening token, so an
  unactivated stream reports zero separated reasoning while the model is
  reasoning hard (that is issue #439 in one sentence). A streaming probe would
  therefore measure the parser and report a working off-switch for every model.
  The non-streaming parser separates the block in all three arms, which is what
  makes the comparison about *generation*.
* **Only separated reasoning is counted.** A model that leaks its thoughts inline
  into ``content`` is measured as producing none, which yields ``no_reasoning``
  and no control. That is the safe direction: no toggle, and today's behaviour.
* **The verdict is keyed to (provider, base_url, model), not to a config row.**
  It is a fact about the software behind the endpoint, so two users pointing at
  the same vLLM share one measurement, and changing the model on a config makes
  the old verdict simply unfindable rather than stale.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.constants import LLM_REASONING_CAPABILITY_KEY_PREFIX
from app.core.constants import LLM_REASONING_PROBE_MAX_TOKENS
from app.core.constants import LLM_REASONING_PROBE_MIN_CHARS
from app.core.constants import LLM_REASONING_PROBE_PROMPT
from app.core.constants import LLM_REASONING_PROBE_SUPPRESSION_RATIO
from app.core.constants import LLM_REASONING_PROBE_TEMPERATURE
from app.core.constants import LLM_REASONING_PROBE_TIMEOUT_S
from app.core.enums import ReasoningOffSwitch
from app.services.llm_service import LLMConfig
from app.services.llm_service import LLMProvider
from app.services.llm_service import LLMService

logger = logging.getLogger(__name__)

__all__ = [
    "PROBEABLE_PROVIDERS",
    "ReasoningOffSwitch",
    "ReasoningProbeResult",
    "capability_key",
    "probe",
    "read",
    "read_record",
    "reasoning_chars",
    "record",
    "resolve_enable_thinking",
    "verdict_of",
    "verdict_from_arms",
]


#: Providers whose "do not reason" parameter this module knows how to send AND
#: has been able to measure. Scoped exactly like
#: ``llm_stream.USAGE_OPTION_PROVIDERS`` and for the same reason: a "custom"
#: OpenAI-clone answers 400 to an unknown payload key, so a probe that dialled
#: one blindly would break the very deployments least able to debug it.
#:
#: Ollama's native ``think`` option and Anthropic's ``thinking.type`` are real
#: mechanisms and belong here — but adding one means sending an untested
#: parameter to a server nobody has measured, which is the exact mistake this
#: module exists to prevent. An entry is earned by a measurement, not by
#: reading a vendor's documentation. Adding one is a line here plus a line in
#: the matching ``LLMService._prepare_*_payload``.
PROBEABLE_PROVIDERS: frozenset[LLMProvider] = frozenset({LLMProvider.VLLM})


@dataclass(frozen=True)
class ReasoningProbeResult:
    """One three-arm measurement, and the verdict derived from it."""

    off_switch: ReasoningOffSwitch
    reasoning_chars_on: int = 0
    reasoning_chars_off: int = 0
    reasoning_chars_omitted: int = 0
    detail: str = ""

    @property
    def control_renders(self) -> bool:
        """Whether the chat UI may offer a reasoning toggle for this model."""
        return self.off_switch is ReasoningOffSwitch.WORKS


def capability_key(provider: str, base_url: str | None, model: str) -> str:
    """`SystemSettings` key holding the verdict for one endpoint+model.

    The identity is the triple, hashed: the verdict is a property of the
    software answering at that URL running that model, not of whoever
    configured it. Two users sharing a vLLM therefore share one measurement,
    and editing a config's model produces a key that has never been written —
    a miss, which reads as "unprobed", rather than a stale verdict for the
    previous model.

    The URL is *hashed rather than stored* so the deployment-wide settings
    table does not accumulate a directory of every user's private endpoints.

    Args:
        provider: Provider value (``LLMProvider``).
        base_url: Configured endpoint, or None for a provider default.
        model: Model name as sent on the wire.

    Returns:
        A fully-qualified `SystemSettings` key.
    """
    identity = f"{provider}|{(base_url or '').strip().rstrip('/')}|{model}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"{LLM_REASONING_CAPABILITY_KEY_PREFIX}{digest}"


def reasoning_chars(message: dict) -> int:
    """Total separately-reported reasoning characters in one response message.

    **Both spellings are summed.** vLLM 0.19 reports ``reasoning``; the vLLM
    reasoning-parser convention and several gateways report
    ``reasoning_content``. An earlier probe read only the latter, measured 0 on
    every arm, and reported the result as non-deterministic noise.

    Args:
        message: ``choices[0].message`` from a non-streaming response.

    Returns:
        Character count, 0 when the model reported none.
    """
    total = 0
    for field in ("reasoning", "reasoning_content"):
        value = message.get(field)
        if isinstance(value, str):
            total += len(value)
    return total


def verdict_from_arms(on: int, off: int, omitted: int) -> tuple[ReasoningOffSwitch, str]:
    """Turn three reasoning-character counts into a verdict.

    Two conditions, both required, because either alone has a way of being
    satisfied by a model with no off-switch:

    1. ``off`` is at most :data:`LLM_REASONING_PROBE_SUPPRESSION_RATIO` of the
       **omitted control** — this is the comparison that catches the measured
       failure, where "off" and "not asking" are the same request.
    2. ``off`` is at most that same fraction of the **activated** arm — without
       it, a model that happens to reason little on the control run would score
       a working off-switch from a coincidence.

    Args:
        on: Reasoning characters with the switch explicitly on.
        off: Reasoning characters with the switch explicitly off.
        omitted: Reasoning characters with the parameter absent.

    Returns:
        ``(verdict, human-readable detail)``.
    """
    ratio = LLM_REASONING_PROBE_SUPPRESSION_RATIO
    if max(on, omitted) < LLM_REASONING_PROBE_MIN_CHARS:
        return (
            ReasoningOffSwitch.NO_REASONING,
            f"the model reported no separated reasoning in any arm "
            f"(on={on}, off={off}, omitted={omitted})",
        )
    if off <= ratio * omitted and off <= ratio * on:
        return (
            ReasoningOffSwitch.WORKS,
            f"off removed the reasoning (on={on}, off={off}, omitted={omitted})",
        )
    return (
        ReasoningOffSwitch.ABSENT,
        f"off did not suppress reasoning (on={on}, off={off}, omitted={omitted})",
    )


def _probe_arm(service: LLMService, enable_thinking: bool | None) -> int:
    """Run one arm and return its separated-reasoning character count.

    ``enable_thinking=None`` omits the parameter entirely — the control arm —
    which is a different request from sending ``False`` and is exactly the
    difference the probe is measuring.
    """
    data = service.chat_completion_raw(
        [{"role": "user", "content": LLM_REASONING_PROBE_PROMPT}],
        timeout=LLM_REASONING_PROBE_TIMEOUT_S,
        temperature=LLM_REASONING_PROBE_TEMPERATURE,
        max_tokens=LLM_REASONING_PROBE_MAX_TOKENS,
        enable_thinking=enable_thinking,
    )
    choices = data.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return 0
    message = choices[0].get("message")
    return reasoning_chars(message) if isinstance(message, dict) else 0


def probe(config: LLMConfig) -> ReasoningProbeResult:
    """Measure whether ``config``'s model honours a "do not reason" parameter.

    Costs **three real generations** against the configured endpoint, which is
    why nothing calls it per request — see ``run_and_record``'s caller.

    Args:
        config: The LLM configuration to measure, unmodified.

    Returns:
        The verdict plus the three arms it was derived from. Never raises: an
        endpoint that refuses, errors, or times out yields ``UNKNOWN``, which
        renders no control and leaves behaviour exactly as it is today.
    """
    if config.provider not in PROBEABLE_PROVIDERS:
        return ReasoningProbeResult(
            off_switch=ReasoningOffSwitch.UNSUPPORTED,
            detail=f"{config.provider} has no off-switch parameter this build can send",
        )

    service = LLMService(config)
    try:
        on = _probe_arm(service, True)
        off = _probe_arm(service, False)
        omitted = _probe_arm(service, None)
    except Exception as exc:  # noqa: BLE001 — any failure must degrade, never raise
        logger.warning("Reasoning probe failed for %s/%s: %s", config.provider, config.model, exc)
        return ReasoningProbeResult(
            off_switch=ReasoningOffSwitch.UNKNOWN,
            detail=f"probe could not complete: {type(exc).__name__}",
        )
    finally:
        service.close()

    off_switch, detail = verdict_from_arms(on, off, omitted)
    logger.info("Reasoning probe %s/%s: %s (%s)", config.provider, config.model, off_switch, detail)
    return ReasoningProbeResult(
        off_switch=off_switch,
        reasoning_chars_on=on,
        reasoning_chars_off=off,
        reasoning_chars_omitted=omitted,
        detail=detail,
    )


def record(db: Session, config: LLMConfig, result: ReasoningProbeResult) -> None:
    """Persist one probe verdict against its model fingerprint.

    Stored as a `SystemSettings` row, but it is a **measurement, not a
    setting**: no coded default is editable into it, no admin panel writes it,
    and an operator who hand-edited the row would be asserting a capability
    nobody measured — the precise failure the whole feature guards against.
    `SystemSettings` is used because the fact is deployment-wide (it belongs to
    the endpoint, not the account) and because keying by fingerprint gives
    invalidation for free, without a migration or a table whose only column is
    a verdict.

    Args:
        db: Session to write through.
        config: The configuration that was probed.
        result: What the probe concluded.
    """
    from app.services.system_settings_service import set_setting

    key = capability_key(str(config.provider), config.base_url, config.model)
    payload = {
        "off_switch": str(result.off_switch),
        "provider": str(config.provider),
        "model": config.model,
        "reasoning_chars": {
            "on": result.reasoning_chars_on,
            "off": result.reasoning_chars_off,
            "omitted": result.reasoning_chars_omitted,
        },
        "detail": result.detail,
        "probed_at": datetime.now(UTC).isoformat(),
    }
    set_setting(
        db,
        key,
        json.dumps(payload),
        description="Measured reasoning off-switch capability (issue #64) — probe output, not an editable setting",
    )


def read_record(db: Session, provider: str, base_url: str | None, model: str) -> dict:
    """The stored probe record for one endpoint+model, or ``{}``.

    Args:
        db: Session to read through (served by the settings TTL cache).
        provider: Provider value.
        base_url: Configured endpoint, or None.
        model: Model name.

    Returns:
        The decoded record, or an empty dict when nothing was recorded or what
        was recorded cannot be read. Both cases mean "unprobed" downstream.
    """
    from app.services.system_settings_service import get_setting

    key = capability_key(provider, base_url, model)
    try:
        raw = get_setting(db, key)
    except Exception as exc:  # noqa: BLE001 — a capability read must not break chat
        logger.warning("Reasoning capability read failed for %s: %s", key, exc)
        return {}
    if not raw:
        return {}
    try:
        stored = json.loads(raw)
    except ValueError:
        logger.warning("Discarding unreadable reasoning capability record at %s", key)
        return {}
    return stored if isinstance(stored, dict) else {}


def verdict_of(stored: dict) -> ReasoningOffSwitch:
    """The verdict a stored record carries, or UNKNOWN.

    A record written by a *newer* build can name a verdict this one has never
    heard of. That must read as UNKNOWN — no control — rather than raise:
    downgrading a deployment should lose the toggle, not the chat page.

    Args:
        stored: A record from :func:`read_record`.

    Returns:
        The recorded verdict, or :attr:`ReasoningOffSwitch.UNKNOWN`.
    """
    raw = stored.get("off_switch")
    if not isinstance(raw, str):
        return ReasoningOffSwitch.UNKNOWN
    try:
        return ReasoningOffSwitch(raw)
    except ValueError:
        return ReasoningOffSwitch.UNKNOWN


def read(db: Session, provider: str, base_url: str | None, model: str) -> ReasoningOffSwitch:
    """The recorded verdict for one endpoint+model, or UNKNOWN.

    Args:
        db: Session to read through.
        provider: Provider value.
        base_url: Configured endpoint, or None.
        model: Model name.

    Returns:
        The recorded verdict. A missing row, unparseable JSON, and a value this
        build does not recognise all return
        :attr:`ReasoningOffSwitch.UNKNOWN`, which renders no control.
    """
    return verdict_of(read_record(db, provider, base_url, model))


def resolve_enable_thinking(
    db: Session, service: LLMService, requested: bool | None
) -> bool | None:
    """Translate a user's reasoning preference into a payload argument.

    The preference is honoured **only** where the probe proved the off-switch
    works. Everywhere else the request is built exactly as it is today, so a
    model with no off-switch keeps receiving vLLM's ``enable_thinking: true``
    and issue #439 stays fixed.

    Args:
        db: Session for the capability read.
        service: The resolved LLM service for this turn.
        requested: ``False`` = the user asked for no reasoning; ``True`` or
            ``None`` = leave the request alone.

    Returns:
        ``False`` to send the measured "off" arm, or ``None`` meaning "pass
        nothing, build the payload as usual".
    """
    if requested is not False:
        return None
    verdict = read(db, str(service.config.provider), service.config.base_url, service.config.model)
    if verdict is not ReasoningOffSwitch.WORKS:
        logger.debug(
            "Ignoring reasoning-off preference: %s/%s reports off_switch=%s",
            service.config.provider,
            service.config.model,
            verdict,
        )
        return None
    return False
