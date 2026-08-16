"""Stage 1 RAG evaluation harness (issue #403).

Modules:

``metrics``       trec_eval via ``pytrec_eval_terrier``; tie normalisation and
                  ``-c`` semantics live here and nowhere else.
``qrels``         gold turn ranges -> chunk-level graded judgements. One adapter
                  for QMSum and the synthetic tier.
``corpora``       query + gold loading, mapped onto the uuids the app indexed.
``index_reader``  seed -> refresh -> force-merge, and reading chunks back.
``runner``        drives the production chat retrieval path.
``answers``       answer scoring for the aggregation class: exact match, the
                  partial-credit rule, and the zero for an unanswered query.
``answerers``     where an aggregation answer comes from — OpenSearch aggs and
                  Postgres, never a language model (D6).
``report``        deterministic results document and metric table.

The metric engine is an **eval-only dependency** (``requirements-eval.txt``) and
is never installed into a published image; see the licence note in that file.
Importing this package does not import it — only :func:`metrics.evaluate` does,
so tests that do not measure anything still collect on a plain install.
"""
