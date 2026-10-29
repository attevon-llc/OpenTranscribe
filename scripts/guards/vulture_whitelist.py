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
)
