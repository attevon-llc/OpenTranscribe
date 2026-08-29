"""Fail-closed gate for transcript text that is about to leave the deployment.

Three background tasks post transcript text to whatever provider the owner
configured — summarization, speaker identification and topic extraction. When
the effective policy says ``redact_before_llm``, that text must be masked first,
and "masked" has to actually mean masked: a path that quietly emits the original
text on a miss is worse than having no policy, because the setting reads as on.

Two conditions defeat naive masking, and both are the *common* case rather than
the edge case:

* **Detection has not finished.** ``redaction_detect_task`` is dispatched by the
  same post-processing step that dispatches these three tasks
  (``tasks/transcription/postprocess.py``), onto a separate CPU queue. Nothing
  orders them, so the LLM tasks routinely reach a transcript whose
  ``TranscriptSegment.redactions`` are still NULL — and
  ``RedactionService.mask_segment`` with an empty span list masks nothing and
  returns the text unchanged. The call looks masked and isn't.
* **Masking raised.** Returning the input on an exception hands the provider
  exactly the content the policy exists to withhold.
* **Detection finished without running a detector.** ``redaction_status = done``
  means the scan completed, not that every detector examined the text: an
  unavailable one is a reported *skip* and still reaches ``done``. Masking then
  applies a complete-looking span cache that simply has no PII in it, so the
  provider receives the transcript verbatim while every log line says masked.
  ``media_file.redaction_coverage`` (v392) is what tells the two apart; see
  ``services/redaction/coverage.py``.

``resolve_llm_masking`` collapses all three into one decision a caller cannot get
subtly wrong, and :class:`RedactionNotReadyError` gives batch callers the option
interactive chat does not have: come back later. Chat cannot wait for a
detection pass mid-request, so it masks inline instead
(``services/chat/redactor.py``) — same guarantee, different tradeoff.
"""

from __future__ import annotations

import ipaddress
import logging
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.core import constants as C  # noqa: N812
from app.services.redaction.config import EffectiveRedactionConfig
from app.services.redaction.config import resolve_effective_config
from app.services.redaction.coverage import describe_gap
from app.services.redaction.coverage import uncovered_detectors

logger = logging.getLogger(__name__)

#: Provider names (matching ``LLMService.LLMProvider``'s string values) that are
#: ELIGIBLE for a ``base_url`` locality check — self-hosted-inference-server
#: shapes whose endpoint is nonetheless genuinely free-form (issue: a ``vllm``
#: config can legitimately point at a hosted vLLM SaaS). Compared by value, not
#: by importing the enum, so this module never has to import ``llm_service`` at
#: runtime — see :func:`is_local_provider`. ``custom`` is handled alongside
#: these because it takes the identical check.
_LOCAL_HOSTED_PROVIDERS = frozenset({"vllm", "ollama"})

#: Hostnames that name "this machine" without needing a DNS round trip.
_LOCAL_HOSTNAMES = frozenset({"localhost", "localhost.localdomain"})


class RedactionNotReadyError(Exception):
    """The policy requires masking but trustworthy spans are unavailable.

    Raised instead of returning ``None``, because ``None`` is the legitimate
    "no masking required" answer — conflating the two is precisely the bug this
    module exists to prevent.

    Attributes:
        retryable: True when detection is still in flight and deferring the task
            will resolve it; False when detection failed and waiting won't help.
        file_id: File awaiting detection, so the deferring caller can kick off a
            scan that was never dispatched.
        never_started: True when ``redaction_status`` was NULL — nothing is
            running, so waiting alone would never resolve.
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        file_id: int | None = None,
        never_started: bool = False,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.file_id = file_id
        self.never_started = never_started


def resolve_llm_masking(db: Session, media_file) -> EffectiveRedactionConfig | None:
    """Decide how to mask a file's transcript before it goes to an LLM provider.

    Args:
        db: Database session.
        media_file: The ``MediaFile`` whose transcript is being sent.

    Returns:
        The config to pass to the transcript builders, or ``None`` when the
        owner's policy does not require pre-LLM masking (send text as-is).

    Raises:
        RedactionNotReadyError: Masking is required but cached spans are missing,
            untrustworthy, or do not cover a category this policy masks. Never
            downgrade this to "send unmasked".
        Exception: Whatever ``resolve_effective_config`` raises. An unresolvable
            policy must not be read as an absent policy — if we cannot tell
            whether masking is required, we must not send.
    """
    cfg = resolve_effective_config(db, int(media_file.user_id))
    if not (cfg.enabled and cfg.redact_before_llm):
        return None

    status = getattr(media_file, "redaction_status", None)
    if status == C.REDACTION_STATUS_DONE:
        # A finished scan is not necessarily a complete one. Masking a file whose PII
        # detector never ran produces a transcript that is untouched, from a call the
        # caller has every reason to believe masked it.
        gap = uncovered_detectors(media_file, cfg)
        if not gap:
            return cfg
        # NOT retryable, for the same reason ``detect_and_store`` records unavailability
        # as a skip rather than a failure: re-running the scan does not install the
        # missing dependency, so deferring would only burn ten attempts and arrive at
        # this refusal anyway. The operator has to act, and the message says what to do.
        logger.error(
            "Refusing to send an incompletely scanned transcript to an LLM provider: %s",
            describe_gap(media_file, gap),
        )
        raise RedactionNotReadyError(
            f"Redaction detection for file {media_file.id} completed without detectors "
            f"{sorted(gap)}, whose categories this policy masks; refusing to send a "
            "transcript that was never examined for them",
            retryable=False,
            file_id=int(media_file.id),
        )

    if status == C.REDACTION_STATUS_FAILED:
        raise RedactionNotReadyError(
            f"Redaction detection failed for file {media_file.id}; "
            "refusing to send unmasked transcript to an LLM provider",
            retryable=False,
            file_id=int(media_file.id),
        )

    # pending / processing / NULL. A NULL status means detection was never
    # dispatched — the owner turned redaction on after upload, and the scan is
    # otherwise only queued lazily when they next open the file. Waiting alone
    # would time out, so the caller dispatches it (see defer_for_redaction).
    raise RedactionNotReadyError(
        f"Redaction detection for file {media_file.id} is {status or 'not started'}; "
        "deferring the LLM call until spans are cached",
        retryable=True,
        file_id=int(media_file.id),
        never_started=status is None,
    )


def defer_for_redaction(task, exc: RedactionNotReadyError, *, countdown: int = 60) -> None:
    """Re-queue a bound Celery task until detection lands, or give up safely.

    Args:
        task: The bound task (``self`` in a ``bind=True`` task).
        exc: The raised readiness error.
        countdown: Seconds to wait before the next attempt.

    Raises:
        Retry: To defer the task (Celery's normal control-flow exception).
        RedactionNotReadyError: When deferring is pointless or retries are exhausted,
            so the task fails loudly instead of silently leaking.
    """
    if not exc.retryable:
        raise exc

    retries = task.request.retries or 0
    if retries >= C.REDACTION_LLM_MAX_DEFERRALS:
        logger.error("Giving up after %d deferrals waiting for redaction spans: %s", retries, exc)
        raise exc

    # Nothing is scanning this file, so deferring alone would just burn retries.
    # Only on the first attempt: redaction_detect_task recomputes spans from
    # scratch, and re-dispatching every 60s would pile up duplicate CPU scans.
    if exc.never_started and retries == 0 and exc.file_id is not None:
        _dispatch_detection(exc.file_id)

    logger.info("Deferring %s for redaction (attempt %d): %s", task.name, retries + 1, exc)
    raise task.retry(exc=exc, countdown=countdown, max_retries=C.REDACTION_LLM_MAX_DEFERRALS)


def _dispatch_detection(file_id: int) -> None:
    """Queue the scan this file never got. Best-effort — the deferral stands either way."""
    try:
        from app.tasks.redaction_task import redaction_detect_task

        redaction_detect_task.delay(file_id=file_id)
        logger.info("Dispatched missing redaction detection for file %s", file_id)
    except Exception:  # noqa: BLE001
        logger.exception("Could not dispatch redaction detection for file %s", file_id)


def is_local_provider(config: object) -> bool:
    """Does this LLM configuration reach a model running on our own deployment?

    Owner decision, 2026-08-13 (see this repo's ``chat/CLAUDE.md``): a **local**
    model never has the transcript leave the machine, so masking it before the
    call costs recall and buys nothing; a **remote** provider still gets masked
    text, because that call is a genuine data-egress event. This function is the
    ONE place that decision is keyed off the provider — callers must never infer
    locality from a global setting or a deployment flag.

    ``provider`` being ``vllm`` or ``ollama`` is NOT enough on its own: both are
    genuinely self-hostable, OpenAI-compatible endpoint shapes, but ``base_url``
    is free-form user/operator input for both (``llm_service.py`` builds the
    endpoint straight from it) and the one place ``base_url`` IS validated
    (the SSRF guards, gated by ``LLM_ALLOW_PRIVATE_ENDPOINTS``, default
    ``false``) *refuses* a private endpoint and *permits* a public one on a
    stock deployment — the inverse of what a provider-name-only check assumed.
    A user pointing a ``vllm`` config at a hosted vLLM SaaS must not have their
    transcript unmasked just because the provider name says "vllm".

    So all three of ``vllm``, ``ollama``, and ``custom`` take the identical
    check: is a config's ``base_url`` a loopback / RFC1918 / link-local / IPv6
    ULA address, or a bare "dotless" hostname — the shape of an unqualified
    docker-compose/Kubernetes service name (``http://backend:8000``,
    ``http://mock-llm:5199/v1``), which cannot resolve outside this
    deployment's own network and would otherwise fail DNS in exactly the
    environments (CI, a fresh dev stack) where treating a lookup failure as
    "remote" would be wrong most often. A ``vllm``/``ollama`` config with no
    ``base_url`` at all now reads remote (it used to read local) — that is
    correct and inert: with no endpoint to reach, it cannot serve a model
    either way.

    Every other provider (``openai``, ``anthropic``, ``claude``, ``openrouter``,
    ``bedrock``) is a hosted third-party API by construction and is never local,
    whatever its ``base_url`` says.

    Every other provider (``openai``, ``anthropic``, ``claude``, ``openrouter``,
    ``bedrock``) is a hosted third-party API by construction and is never local,
    whatever its ``base_url`` says.

    **ANY ambiguity resolves to False (remote — mask).** An unparseable
    ``base_url``, a missing one, a hostname that fails to resolve, or one that
    resolves to a public address are all judged remote. This function is a
    safety gate, not a diagnostic: a config it cannot confidently classify as
    local must be treated exactly like a real third-party endpoint.

    Args:
        config: The ``LLMConfig`` in play for this turn, typed ``object`` because
            this function is genuinely structural: every read goes through
            ``getattr(config, "...", default)``, so this module never has to
            import ``llm_service`` at runtime (see ``_LOCAL_HOSTED_PROVIDERS``)
            and an object carrying neither attribute is accepted rather than
            rejected — it simply reads as remote, the same as an unrecognised
            provider string. ``None`` is accepted directly for the same reason
            — a deployment with no ``LLM_PROVIDER`` configured at all is a
            first-class shape (#403 D6: deterministic maps/keyphrase/coverage
            tiers still run with no LLM), and that path has no config to read
            a provider from. Ambiguity fails closed, not a special case.

    Returns:
        True only when every check above resolves to "local"; False otherwise
        (including when ``config`` is ``None``).
    """
    provider = str(getattr(config, "provider", "") or "").strip().lower()
    if provider not in _LOCAL_HOSTED_PROVIDERS and provider != "custom":
        return False
    return _custom_endpoint_is_local(getattr(config, "base_url", None))


def _custom_endpoint_is_local(base_url: str | None) -> bool:
    """Is a ``custom`` provider's ``base_url`` a host that can only be ours?

    Split out of :func:`is_local_provider` so each failure mode (no URL, an
    unparseable one, a hostname that fails to resolve) has one visible ``return
    False`` rather than being buried in a single long function.
    """
    if not base_url:
        return False
    try:
        parsed = urlparse(base_url)
    except ValueError:
        return False
    hostname = (parsed.hostname or "").strip().lower()
    if not hostname:
        return False
    if hostname in _LOCAL_HOSTNAMES:
        return True

    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None:
        return _is_local_address(literal)

    if "." not in hostname:
        # A bare service name — `http://backend:8000`, `http://mock-llm:5199/v1`.
        # Only a docker-compose/K8s service name is shaped like this; a public
        # hostname always has a dot. Judged local WITHOUT a DNS round trip: many
        # of the environments this matters most in (a fresh dev stack, CI, the
        # mock-LLM fixture) have no resolver entry for it outside the app's own
        # network, and treating that lookup failure as "remote" would misclassify
        # the common case rather than the rare one.
        #
        # Guard: an UNBRACKETED IPv6 literal in the URL also lands here — e.g.
        # `urlparse("http://2001:db8::1/v1").hostname` returns `"2001"`, which
        # has no dot and no letters. A real compose/K8s service name is never
        # purely hex digits, so refuse anything that parses as an integer
        # (covers decimal and, since Python 3 int() also accepts hex literals
        # with a 0x prefix, the common malformed-IPv6 shapes) rather than
        # misreading a public address as ours.
        try:
            int(hostname, 16)
        except ValueError:
            return True
        return False

    from app.utils.url_validation import resolve_public_addresses

    # `allow_private=True` here does not loosen anything we care about: it only
    # widens which addresses come BACK (so a private one isn't pre-filtered away
    # before we can judge it ourselves) while still refusing cloud metadata
    # addresses outright — and nothing self-hosts an inference server behind
    # instance metadata, so a metadata verdict is correctly judged remote below.
    addresses, reason = resolve_public_addresses(base_url, allow_private=True)
    if not addresses or reason:
        # DNS failure, malformed URL, or blocked as instance metadata — all
        # ambiguous or definitively not-ours. Fail closed to remote.
        return False
    # Every resolved address must be local — a hostname split between a private
    # and a public A record is exactly the ambiguity this function refuses to
    # guess about.
    return all(_is_local_address(ipaddress.ip_address(addr)) for addr in addresses)


def _is_local_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Loopback / RFC1918 / link-local / IPv6 ULA — the ranges this deployment's
    own network uses. ``is_private`` already covers loopback and link-local for
    both address families; the explicit checks document the ranges by name
    rather than relying on that alone.
    """
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return bool(ip.is_loopback or ip.is_link_local or ip.is_private)
