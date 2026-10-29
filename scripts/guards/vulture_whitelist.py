"""Names vulture must treat as used, each with the reason it stays.

Vulture matches by NAME, so every entry below is a name that production code never calls but
that is kept on purpose. Delete an entry when its name gains a production caller, or when the
function goes.

The usual ``vulture --make-whitelist`` shape is a bare ``_.name`` expression statement. That
trips ruff (F821 undefined ``_``, B018 useless expression) and this repo does not suppress
findings, so the same attribute loads are collected into one tuple instead. Vulture counts
an attribute load as a use of the name either way.
"""

from types import SimpleNamespace

_ = SimpleNamespace()

WHITELIST = (
    # Stage 4's citation builder is the documented caller (document digests have no timestamp,
    # so None selects a file-level citation); the provenance tests pin both arms of the contract.
    _.provenance_timespan,
    # The embedding-window tests assert real digest sizing through it (integration and digest
    # unit tests); removing it would remove those assertions, not just the helper.
    _.fits_embedding_window,
    # Its test pins that failure text is sanitised before it is stored and notified, through
    # the real retry-policy path (#959); the only coverage of that outer-exception shape.
    _._handle_outer_exception,
    # Reader half of the infra-requeue budget; the orphaned-delivery live test uses it to prove
    # a user-cancelled run spends none of the budget.
    _.infra_requeues,
    # Observation point for the cancellation scope; the cooperative-cancel tests use it to
    # prove the scope neither leaks across threads nor outlives its context manager.
    _.current_cancellation_task_id,
    # Read-only view of the poisoned-CUDA-context flag; the poisoned-exit tests assert it.
    _.context_poisoned,
    # State accessors the VRAM and host-memory admission tests use to observe reservations
    # against the real singleton.
    _.current_budget,
    _.current_gate,
    # Used only inside a quoted cast("CursorResult[Any]", ...) annotation on the same module,
    # which vulture does not read as a use; ruff would flag removing the import.
    _.CursorResult,
    # Re-export kept for the PKI suite, which imports this name from app.auth.pki_auth
    # (test_pki_auth.py and test_proxy_header_auth.py assert it is header_trust.ip_in_networks).
    _._is_pki_trusted_proxy,
    # http.server.BaseHTTPRequestHandler dispatches do_GET, do_POST and log_message by name
    # (the mock LLM and mock ASR servers); nothing calls them directly.
    _.do_GET,
    _.do_POST,
    _.log_message,
    # torch.nn.Module.__call__ dispatches forward by name (the ONNX export spike module).
    _.forward,
    # PEP 562 module-level __getattr__: the interpreter calls it on a missing attribute of
    # app.services.opensearch_service, never a visible call site.
    _.__getattr__,
    # Foundation of the DomainError taxonomy (L2): the status-parameterised coded_error factory in
    # services/support_access_service.py is its production caller once that site is converted; until
    # then only test_domain_errors.py pins the status-to-class mapping.
    _.domain_error_for_status,
)
