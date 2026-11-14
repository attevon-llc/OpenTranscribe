"""Make the PII analyzer resident before a request, the way a running deployment has it.

A request thread never builds a detector model: inside ``detectors.no_model_load`` a cold
analyzer reports unavailable and the masker fails closed with a 503 (``8257d6424`` for summary
masking, ``150d0035d`` for the inline chat masker). A deployment pays the build off the request,
in ``redaction.warmup.start_pii_warmup`` at startup. A test that drives live detection through
an endpoint must therefore warm the analyzer first, or its result depends on whether an earlier
test in the same xdist worker happened to leave the model resident.

Asserts rather than skips: these tests carry the ``models`` marker, so a missing model is a
broken environment to report, not a reason to pass vacuously.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def resident_pii_analyzer() -> None:
    """Build the Presidio analyzer with the same warm-up the API process runs at startup."""
    from app.services.redaction.warmup import warm_pii_analyzer

    assert warm_pii_analyzer(), "the Presidio analyzer could not be built in this environment"
