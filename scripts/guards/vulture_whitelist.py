"""Names vulture must treat as used, each with the reason it stays.

Vulture matches by NAME, so every entry below is a name that production code never calls but
that is kept on purpose. Every block of entries sits under a comment carrying a tag
(``[extension-seam]``, ``[test-observation]``, ``[framework-dispatch]`` or ``[operator-entrypoint]``) plus a written
reason; ``check_vulture.py`` fails on a block without both. Delete an entry when its name gains a production caller, or when the
function goes.

The usual ``vulture --make-whitelist`` shape is a bare ``_.name`` expression statement. That
trips ruff (F821 undefined ``_``, B018 useless expression) and this repo does not suppress
findings, so the same attribute loads are collected into one tuple instead. Vulture counts
an attribute load as a use of the name either way.
"""

from types import SimpleNamespace
from typing import Any

_: Any = SimpleNamespace()

WHITELIST = (
    # [test-observation] Stage 4's citation builder is the documented caller (document digests have no timestamp,
    # so None selects a file-level citation); the provenance tests pin both arms of the contract.
    _.provenance_timespan,
    # [test-observation] The embedding-window tests assert real digest sizing through it (integration and digest
    # unit tests); removing it would remove those assertions, not just the helper.
    # [test-observation] Its test pins that failure text is sanitised before it is stored and notified, through
    # the real retry-policy path (#959); the only coverage of that outer-exception shape.
    _._handle_outer_exception,
    # [test-observation] Reader half of the infra-requeue budget; the orphaned-delivery live test uses it to prove
    # a user-cancelled run spends none of the budget.
    # [test-observation] Observation point for the cancellation scope; the cooperative-cancel tests use it to
    # prove the scope neither leaks across threads nor outlives its context manager.
    # [test-observation] Read-only view of the poisoned-CUDA-context flag; the poisoned-exit tests assert it.
    # [test-observation] State accessors the VRAM and host-memory admission tests use to observe reservations
    # against the real singleton.
    # [framework-dispatch] Used only inside a quoted cast("CursorResult[Any]", ...) annotation on the same module,
    # which vulture does not read as a use; ruff would flag removing the import.
    _.CursorResult,
    # [test-observation] Re-export kept for the PKI suite, which imports this name from app.auth.pki_auth
    # (tests/pki_auth/ and test_proxy_header_auth.py assert it is header_trust.ip_in_networks).
    _._is_pki_trusted_proxy,
    # [framework-dispatch] http.server.BaseHTTPRequestHandler dispatches do_GET, do_POST and log_message by name
    # (the mock LLM and mock ASR servers); nothing calls them directly.
    _.do_GET,
    _.do_POST,
    _.log_message,
    # [framework-dispatch] torch.nn.Module.__call__ dispatches forward by name (the ONNX export spike module).
    _.forward,
    # [framework-dispatch] PEP 562 module-level __getattr__: the interpreter calls it on a missing attribute of
    # app.services.opensearch_service, never a visible call site.
    _.__getattr__,
    # [framework-dispatch] Called by the v456 migration (backend/alembic/versions/v456_unique_object_keys.py), which
    # this guard does not scan; the runner never imports it by name.
    _.clear_regenerable_duplicates,
    # [framework-dispatch] The Locust load test (scripts/perf/load/): Locust discovers every User subclass and the
    # LoadTestShape subclass in the locustfile's namespace, instantiates them by class, and
    # calls on_start/on_stop around each simulated user; nothing calls them by name.
    _.ProfileShape,
    _.on_start,
    _.on_stop,
    # [extension-seam] Plug-in registration points: the cloud edition (a separate repository that pins this core) calls them at startup, so no call site exists in this tree. Each is exercised by tests.
    _.set_capability_resolver,
    _.set_allowed_models_resolver,
    _.set_chat_limits_resolver,
    _.set_redaction_floor_resolver,
    _.set_retention_resolver,
    _.set_upload_limits_resolver,
    _.set_privacy_ceiling_resolver,
    _.set_document_provider,
    _.register_verifier,
    _.register_before_message,
    _.register_before_dispatch,
    _.register_transcription_complete,
    _.register_voiceprint_consent_hook,
    _.register_erasure_store,
    _.register_protected_table,
    # [extension-seam] Counterparts of the registration points above (unregister/clear), part of the same cloud-edition contract; tests use them to restore the default seam state.
    _.unregister_verifier,
    _.clear_hooks,
    _.clear_voiceprint_consent_hooks,
    # [extension-seam] Backends are loaded from dotted-path strings in _TRANSCRIBER_REGISTRY / the diarizer registry (importlib), so the class names never appear at a call site; warmup is the backend Protocol method the engine contract defines (issue #366 adds a fourth transcriber here).
    _.get_transcriber_backend,
    _.CloudBackend,
    _.FasterWhisperBackend,
    _.WhisperxBackend,
    _.NativeBackend,
    _.PyAnnoteBackend,
    _.warmup,
    # [extension-seam] Abstract provider capability flags (ASR and diarization ABCs). Every provider and every test stub must implement them, and the provider catalog mirrors the same facts; nothing calls them yet.
    _.supports_vocabulary,
    _.supports_speaker_count,
    # [extension-seam] Documented builder API in api/README.md and the unit tests, and test_no_exception_echo_in_response.py enumerates the ErrorHandler builder names, so removing one changes what that guard checks.
    _.handle_not_found,
    _.database_error,
    _.file_processing_error,
    _.not_found_error,
    _.unauthorized_error,
    # [extension-seam] Mechanism built for the BM25 no-stem preset (#506); adoption by the router layer is a documented, measurement-gated decision (services/chat/language.py module docstring).
    _.resolve_text_field_preset_for_locale,
    # [extension-seam] Restore-default counterparts of set_capability_resolver / set_privacy_ceiling_resolver; both are listed in tests/fixtures/extension_surface.txt, so an out-of-tree extension may call them and they are not test-only.
    _.reset_capability_resolver,
    _.reset_privacy_ceiling_resolver,
    # [test-observation] Retained pending a decision, not because they are right as they are: a test asserts the invariant through each, and no production path was found that does the same job differently (reviewed in L04b).
    _.assert_real_timings,
    _.needs_rehash_for_fips_v3,
    _.generic_guard_tables,
    _.trigger_arguments,
    _.find_missing_pieces,
    _.ChatWarningCode,
    # [operator-entrypoint] Run by a person or a script, never imported by app code: generate_encryption_key is the command the security docs and the entropy error message tell an operator to run; export_word_reference is the in-container reference exporter (issue #193); survey_speaker_id_coverage is the measurement instrument services/search/CLAUDE.md names.
    _.generate_encryption_key,
    _.export_word_reference,
    _.survey_speaker_id_coverage,
    # [operator-entrypoint] Offline benchmark/maintenance script helpers whose logic is pinned by a unit test but whose only caller is a script entry point that vulture does not follow.
    _.pick_cap,
    _.declared_component_license,
    _.UserRow,
)
