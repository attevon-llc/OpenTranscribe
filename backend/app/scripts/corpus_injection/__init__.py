"""Non-ASR corpus injection for the #403 RAG evaluation harness.

Parses a reference meeting corpus into OpenTranscribe's own data model
(``MediaFile`` / ``Speaker`` / ``TranscriptSegment``) and then dispatches the
**production** search-indexing task, so what the harness measures is the real
retrieval stack rather than a parallel one built for the benchmark.

Running ASR over the eval corpora would be both infeasible (232 QMSum meetings
plus ICSI, Earnings and a synthetic corpus) and wrong: Stage 1 measures
retrieval, and ASR variance would contaminate every number in the paper.

Entry point: ``python -m app.scripts.corpus_injection --help``, or the wrapper
``scripts/inject-eval-corpus.sh``. Method and manifest format:
``.rag-403/corpus-injection.md``.
"""
