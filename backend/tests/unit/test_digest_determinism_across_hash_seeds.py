"""The extractive digest must be byte-identical under DIFFERENT hash seeds.

This is deliberately the opposite of pinning ``PYTHONHASHSEED``.

Pinning would make every run agree by removing the variable — but the pin can
only ever be an *overridable deployment env var* (it is read before the
interpreter starts, so nothing in-process can set it), which means production
code can never rely on it and every ``set`` → ``list`` must be ``sorted()``
anyway. Worse, a pin would **mask** the bug it claims to prevent: an unsorted set
would produce stable output in dev and CI, and vary only on a deployment that set
``PYTHONHASHSEED=random``. The failure would surface far from the change that
caused it.

So instead of removing the variable, this test *exercises* it: run the ranker in
two subprocesses under two different seeds and require identical output. An
unsorted set anywhere in the artifact path fails here, immediately, on the commit
that introduces it.

Why it matters at all — measured, not assumed. ``tfidf_matrix`` builds its
vocabulary from a set of tokens, and the vocabulary order is the column order of
the matrix. Column permutation is a no-op for cosine similarity in exact
arithmetic, but float addition is not associative: over 20 random permutations of
a 391-token vocabulary, **0 of 20** produced a bit-identical similarity matrix
(max delta 7.8e-16, ~3.5x float64 eps). That is enough to flip a near-tie in
sentence selection, and the digest is indexed and cited.

Subprocesses are required. ``PYTHONHASHSEED`` is consumed during interpreter
start-up, so setting ``os.environ`` inside a running test changes nothing — a
same-process version of this test would pass unconditionally and prove nothing.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]

#: Enough distinct tokens that an unsorted vocabulary would reorder visibly, and
#: enough near-duplicate sentences that ranking has real ties to break.
_PROBE = textwrap.dedent(
    """
    import json
    import sys

    from app.services.ingest_artifacts.textrank import similarity_matrix, tfidf_matrix

    words = [f"term{i}" for i in range(120)]
    documents = []
    for s in range(40):
        # Deterministic by construction (no RNG): the only thing that varies
        # between the two runs is the interpreter's hash seed.
        documents.append([words[(s * 7 + k * 13) % len(words)] for k in range(1, 18)])

    matrix = tfidf_matrix(documents)
    similarity = similarity_matrix(matrix)

    # repr of the exact float64 values, so this compares BITS, not a rounded view.
    print(json.dumps({
        "shape": list(similarity.shape),
        "values": [repr(float(v)) for v in similarity.ravel()],
    }))
    """
)


def _run_with_seed(seed: str, tmp: Path) -> dict:
    """Run the probe in a fresh interpreter under one hash seed."""
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", _PROBE],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        env={
            "PYTHONHASHSEED": seed,
            "PATH": "/usr/bin:/bin",
            "TESTING": "True",
            # `config.Settings.__init__` mkdirs these and they default to /app/...,
            # which does not exist outside the container. Without them the probe dies
            # on import and the comparison below never runs.
            "DATA_DIR": str(tmp),
            "MODELS_DIR": str(tmp),
            "TEMP_DIR": str(tmp),
        },
    )
    assert result.returncode == 0, (
        f"probe failed under PYTHONHASHSEED={seed}:\n{result.stderr[-2000:]}"
    )
    # Annotated, not returned bare: `json.loads` is Any and would widen the
    # declared return type, which is the shape both callers index into.
    parsed: dict = json.loads(result.stdout.strip().splitlines()[-1])
    return parsed


def test_set_iteration_order_really_does_vary_between_these_seeds():
    """Guard the guard: if the seeds produced the same order, the test below is vacuous.

    A determinism test whose two arms are secretly identical passes no matter how
    broken the code is — the same shape as an auditor whose detector matches
    nothing.
    """
    probe = 'print(list({"alpha","beta","gamma","delta","epsilon","zeta","eta","theta"}))'
    orders = set()
    for seed in ("1", "424242"):
        out = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            timeout=60,
            env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"},
        )
        assert out.returncode == 0, out.stderr
        orders.add(out.stdout.strip())

    assert len(orders) == 2, (
        "both hash seeds produced the same set iteration order, so the determinism "
        "test below is not actually varying anything. Pick different seeds."
    )


def test_the_digest_ranker_is_identical_under_two_different_hash_seeds(tmp_path: Path):
    """The real check: sorted() must make the output independent of the seed."""
    first = _run_with_seed("1", tmp_path)
    second = _run_with_seed("424242", tmp_path)

    assert first["shape"] == second["shape"]
    assert first["values"] == second["values"], (
        "the TF-IDF/similarity output changed with PYTHONHASHSEED, which means some "
        "`set` in the artifact path is being turned into a list without sorted(). "
        "Find it and sort it — do NOT pin PYTHONHASHSEED to hide this."
    )
