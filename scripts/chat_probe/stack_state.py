"""Log in, set the LLM config and chat flags for a run, and restore them afterwards."""

from __future__ import annotations

from typing import Any

from chat_probe.config import logger


def login(session: Any, base_url: str, email: str, password: str) -> None:
    """Authenticate and attach the double-submit CSRF header for every later POST.

    Args:
        session: A ``requests.Session`` to authenticate in place.
        base_url: e.g. ``http://localhost:5274/api``.
        email: Login email.
        password: Login password.

    Raises:
        requests.HTTPError: The login request itself failed.
        SystemExit: Login succeeded but set no ``csrf_token`` cookie — every
            mutating request after this would then 403, which reads as a much
            more confusing failure than refusing here.
    """
    response = session.post(
        f'{base_url}/auth/login',
        data={'username': email, 'password': password},
        timeout=30,
    )
    response.raise_for_status()
    logger.info('login ok: %s cookies=%s', response.status_code, list(session.cookies.keys()))
    csrf = session.cookies.get('csrf_token')
    if not csrf:
        raise SystemExit(
            'login succeeded but no csrf_token cookie was set — every mutating '
            'request below would 403 (backend/app/middleware/csrf.py)'
        )
    session.headers.update({'X-CSRF-Token': csrf})


def ensure_llm_config(
    session: Any,
    base_url: str,
    *,
    name: str,
    provider: str,
    model_name: str,
    llm_base_url: str,
    max_tokens: int,
    temperature: str,
) -> str:
    """Create (or reuse) a UserLLMSettings row for the probe, and make it active.

    Args:
        session: An authenticated ``requests.Session``.
        base_url: The app's API base URL.
        name: Config display name. Reused across runs by matching ``llm_base_url``,
            not by this name, so re-running the probe never accumulates configs.
        provider: e.g. ``"vllm"``, ``"openai"``.
        model_name: Model identifier the provider expects.
        llm_base_url: The provider's OpenAI-compatible base URL, e.g.
            ``http://llm-test-vllm:8000/v1`` — resolved from the BACKEND
            CONTAINER's network, not this script's host.
        max_tokens: Context window declared for the config.
        temperature: Sampling temperature, as the API expects it (a string).

    Returns:
        The configuration's uuid.
    """
    response = session.get(f'{base_url}/llm-settings', timeout=30)
    response.raise_for_status()
    for config in response.json().get('configurations', []):
        if config.get('base_url') == llm_base_url:
            config_uuid = str(config['uuid'])
            # ⚠️ Reuse must RECONCILE, not just adopt. Matching on base_url alone and
            # keeping whatever the row already held silently discarded --llm-max-tokens,
            # and because that field IS the context window (LLMService sets
            # `user_context_window = config.max_tokens`), an arm asking for a 60,000
            # window ran at the pre-existing 8,192 and reported `budget_chars` identical
            # to the control. The arm looked like a null result when it had never been
            # applied — the most expensive kind of wrong answer, because it argues
            # against the change.
            stale = {
                key: (config.get(key), value)
                for key, value in (
                    ('max_tokens', max_tokens),
                    ('model_name', model_name),
                    ('provider', provider),
                )
                if config.get(key) != value
            }
            if stale:
                logger.warning(
                    'existing LLM config %s differs from the requested arm (%s) — updating it',
                    config_uuid,
                    ', '.join(f'{k}: {was!r} -> {want!r}' for k, (was, want) in stale.items()),
                )
                patch = session.put(
                    f'{base_url}/llm-settings/config/{config_uuid}',
                    json={
                        'max_tokens': max_tokens,
                        'model_name': model_name,
                        'provider': provider,
                        'temperature': temperature,
                    },
                    timeout=30,
                )
                patch.raise_for_status()
            else:
                logger.info('reusing existing LLM config %s', config_uuid)
            break
    else:
        payload = {
            'name': name,
            'provider': provider,
            'model_name': model_name,
            'base_url': llm_base_url,
            'max_tokens': max_tokens,
            'temperature': temperature,
            'is_active': True,
            'is_shared': False,
        }
        response = session.post(f'{base_url}/llm-settings', json=payload, timeout=30)
        response.raise_for_status()
        config_uuid = str(response.json()['uuid'])
        logger.info('created LLM config %s', config_uuid)

    response = session.post(
        f'{base_url}/llm-settings/set-active',
        json={'configuration_id': config_uuid},
        timeout=30,
    )
    response.raise_for_status()

    # Read the window BACK from the server and refuse to run on a mismatch. The
    # value decides the whole excerpt budget, so an arm that silently ran at a
    # different one is not a measurement of anything — and every other symptom of
    # that (identical `budget_chars`, unchanged `chunks_used`) reads as "the change
    # did nothing" rather than "the change never happened".
    check = session.get(f'{base_url}/llm-settings', timeout=30)
    check.raise_for_status()
    for config in check.json().get('configurations', []):
        if str(config.get('uuid')) == config_uuid:
            resolved = config.get('max_tokens')
            if resolved != max_tokens:
                raise SystemExit(
                    f'LLM config {config_uuid} reports max_tokens={resolved!r} after an update '
                    f'to {max_tokens!r}. Refusing to run: this field IS the context window, so '
                    f'the arm would silently measure a different budget than it claims.'
                )
            logger.info(
                'ACTIVE LLM config %s — model=%s provider=%s CONTEXT WINDOW=%s',
                config_uuid,
                config.get('model_name'),
                config.get('provider'),
                resolved,
            )
            break
    return config_uuid


def ensure_chat_flags(session: Any, base_url: str, updates: dict[str, bool]) -> dict[str, bool]:
    """PUT ``chat.*`` ``SystemSettings`` flags and refuse to run on a read-back mismatch.

    Args:
        session: An authenticated ``requests.Session``.
        base_url: The app's API base URL.
        updates: ``{field: value}`` pairs matching ``ChatAdminSettingsUpdate`` fields
            (e.g. ``speaker_resolver_enabled``), never the ``SystemSettings`` key.

    Returns:
        The requested fields, read back from ``GET /admin/chat-settings`` after the
        write — i.e. what the run actually used, not what it asked for.

    Raises:
        SystemExit: A read-back value disagrees with what was requested.

    Chat ``SystemSettings`` flags have **no** read-back guard of their own — unlike
    the LLM config (:func:`ensure_llm_config`), nothing in the chat pipeline refuses
    to start on a stale value. Issue #523's own ``ami81-routerfix`` artifact is the
    reason this exists: every one of 81 turns showed ``route.speaker_focus: false``,
    including turns using a lexicon-covered verb, because
    ``chat.speaker_resolver_enabled`` was never actually flipped on the stack that
    produced it — an operator step this function now performs and verifies instead
    of leaving to be done, or forgotten, by hand. A silently-unapplied flag produces
    a clean, plausible, meaningless table, and because a null result *argues against*
    the change, it is the most expensive kind of wrong answer (see the CW-1
    precondition on :func:`ensure_llm_config` for the sibling trap on the LLM side).
    """
    if not updates:
        return {}
    put = session.put(f'{base_url}/admin/chat-settings', json=dict(updates), timeout=30)
    put.raise_for_status()

    check = session.get(f'{base_url}/admin/chat-settings', timeout=30)
    check.raise_for_status()
    resolved = check.json()

    mismatches = {
        field: (resolved.get(field), want)
        for field, want in updates.items()
        if resolved.get(field) != want
    }
    if mismatches:
        detail = ', '.join(
            f'{k}: requested {want!r}, read back {was!r}' for k, (was, want) in mismatches.items()
        )
        raise SystemExit(
            f'chat flag read-back mismatch after PUT /admin/chat-settings: {detail}. '
            'Refusing to run: this is exactly the applied-check failure mode issue #523 '
            'documented — a run against a flag that never actually flipped produces a '
            'clean, meaningless table that reads as a null result.'
        )
    read_back = {field: resolved[field] for field in updates}
    logger.info('chat flags confirmed via read-back: %s', read_back)
    return read_back


def snapshot_chat_flags(session: Any, base_url: str, fields: list[str]) -> dict[str, Any]:
    """The current value of each chat flag this run is about to change, for restoring."""
    if not fields:
        return {}
    response = session.get(f'{base_url}/admin/chat-settings', timeout=30)
    response.raise_for_status()
    current = response.json()
    return {field: current.get(field) for field in fields}


def active_llm_config(session: Any, base_url: str) -> str | None:
    response = session.get(f'{base_url}/llm-settings', timeout=30)
    response.raise_for_status()
    active = response.json().get('active_configuration_id')
    return str(active) if active else None


def restore_state(
    session: Any,
    base_url: str,
    *,
    chat_flags: dict[str, Any],
    prior_llm: str | None,
    current_llm: str | None,
    conversations: list[str],
) -> None:
    """Undo what the run changed. Best effort per item, each failure logged with what to fix."""
    for conversation_uuid in conversations:
        try:
            session.delete(f'{base_url}/chat/conversations/{conversation_uuid}', timeout=30)
        except Exception as exc:  # noqa: BLE001 - keep restoring the rest
            logger.warning('could not delete probe conversation %s: %s', conversation_uuid, exc)
    if chat_flags:
        try:
            session.put(f'{base_url}/admin/chat-settings', json=chat_flags, timeout=30)
            logger.info('restored chat flags: %s', chat_flags)
        except Exception as exc:  # noqa: BLE001 - report, do not mask the run's own result
            logger.warning('could not restore chat flags %s: %s', chat_flags, exc)
    if prior_llm and prior_llm != current_llm:
        try:
            session.post(
                f'{base_url}/llm-settings/set-active',
                json={'configuration_id': prior_llm},
                timeout=30,
            )
            logger.info('restored active LLM config %s', prior_llm)
        except Exception as exc:  # noqa: BLE001 - report, do not mask the run's own result
            logger.warning('could not restore active LLM config %s: %s', prior_llm, exc)
