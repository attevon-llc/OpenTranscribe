#!/bin/bash
# OpenTranscribe — Tier 2 corpus fetcher for the RAG evaluation harness (issue #403)
#
# Downloads the PUBLIC meeting/speech corpora the harness scores retrieval against:
#
#   qmsum  QMSum (Yale-LILY), MIT — 232 meeting transcripts with speaker labels,
#          1,810 human queries, each specific query carrying gold relevant turn
#          spans. This is what nDCG@k / recall@k are computed from.
#   ami    AMI Meeting Corpus manual (NXT) annotations v1.6.2, CC BY 4.0 —
#          word-level transcripts WITH TIMESTAMPS + speaker/role metadata for
#          171 meetings. TRANSCRIPTS AND ANNOTATIONS ONLY: the ~100 h of audio
#          and video is NOT fetched (it is hundreds of GB and the harness never
#          runs ASR — it ingests reference transcripts).
#   icsi   ICSI Meeting Corpus NXT annotations, CC BY 4.0 — the OTHER half of
#          icsi-orig  QMSum. All 59 QMSum `Academic` meetings are ICSI meetings, so
#          this does for the Academic domain exactly what ami does for Product:
#          supplies real word-level timings and per-speaker channels for meetings
#          that already have queries and gold spans. 75 meetings, 5–11 speakers.
#   earnings  Rev.com Earnings-21 + Earnings-22 TRANSCRIPTS, CC BY-SA 4.0 — 169
#          real corporate earnings calls (~158 h), per-token speaker ids, real
#          speaker names, sector metadata, entity tags, RTTM diarization refs.
#          The only BUSINESS-domain speech here and the only Tier A haystack that
#          is neither academic nor governmental.
#   meetingbank  MeetingBank (Hu et al., ACL 2023) — TIER B, CC BY-NC-ND 4.0.
#          1,366 US city-council meetings, ~31.7 M words: an order of magnitude
#          more haystack than everything else combined, with real timestamps and
#          diarized speakers. Local use only — see the tiering note below.
#   elitr  ELITR Minuting Corpus, TIER B, CC BY-NC-SA 4.0 — 179 meetings with 403
#          human-written minutes AND manual transcript↔minute span alignments.
#          Already pseudonymised ([PERSON5]), which also exercises redaction.
#
# Data lands OUTSIDE the repo (default /mnt/nas/opentranscribe-benchmarks) and
# must never be committed: these are large third-party corpora and this repo is
# public. Keep them on the NAS.
#
# RETRIEVAL BENCHMARK SUITES (land under <root>/retrieval-benchmarks/):
#
#   locov1     LoCoV1 (HazyResearch), Apache-2.0 — the qmsum and summ_screen_fd
#              subsets. LoCo PUBLISHES nDCG@10 on QMSum, so this is what lets the
#              paper situate its number against a published one.
#   longembed  LongEmbed (EMNLP 2024) qmsum subset — a SECOND published nDCG@10 on
#              QMSum, in clean BEIR corpus/queries/qrels form. TIER B: the packaging
#              carries NO licence grant of any kind (neither the HF card nor the
#              GitHub repo states one), so treat it as local-validation only and
#              rebuild the task from QMSum (MIT) for anything published.
#
# MULTILINGUAL / CROSS-LINGUAL ARMS (issue #403, land under <root>/multilingual/):
#
#   miracl miracl-corpus   MIRACL, Apache-2.0 — 18 languages, human graded qrels.
#                          The anchor: everything the paper says about non-English
#                          retrieval is scored off this.
#   ciral ciral-corpus     CIRAL, Apache-2.0 — English queries → Hausa/Somali/
#                          Swahili/Yoruba passages, human qrels (African CLIR).
#   mrtydi mrtydi-corpus   Mr. TyDi, Apache-2.0 — 11 languages, a SECOND independent
#                          human-judged benchmark over MIRACL's languages.
#   mldr                   MLDR, MIT — 13 languages of LONG documents. Queries are
#                          GPT-3.5-generated, so its judgements are synthetic.
#   lareqa                 XQuAD-R, CC BY-SA 4.0 — 11-way parallel sentence retrieval.
#   multi-eup              Multi-EuP, Apache-2.0 — 24 EU languages, structural qrels.
#   voxpopuli-asr fleurs   CC0 / CC BY 4.0 speech TRANSCRIPTS (no audio) — no qrels,
#                          multilingual ingest realism only.
#   qrecc                  QReCC, CC BY 3.0 — English conversational retrieval.
#   xlsum                  XL-Sum, CC BY-NC-SA 4.0 — TIER B. 45 languages of
#                          summarisation. Usable locally; NEVER publish a number
#                          derived from it. Needs --accept-noncommercial.
#
# GENERAL-IR SLICE (BEIR, issue #403, land under <root>/beir/):
#
#   Exactly TWO tasks, both Tier A, both verified against the ORIGINAL corpus's own
#   terms rather than BEIR's packaging label. A retrieval paper reporting zero BEIR
#   numbers reads as parochial; the whole suite is neither needed nor licensable.
#
#   beir-hotpotqa  HotpotQA, CC BY-SA 4.0 — 5,233,329 Wikipedia paragraphs, 7,405 test
#                  queries, multi-hop (2.0 gold docs/query). The SCALE arm: it is the
#                  only Tier A task here that proves the hybrid BM25+kNN+RRF stack
#                  survives a five-million-document index.
#   beir-scifact   SciFact, CC BY 4.0 (claims) + ODC-By 1.0 (abstracts) — 5,183
#                  abstracts, 300 test queries. The LONG-DOCUMENT arm: 213.6 words per
#                  document is the third-longest in BEIR's public set and the closest
#                  of any Tier A task to our transcript chunk size. 2.7 MB.
#
#   ⚠️ EVERY `BeIR/*` HuggingFace repo is tagged `cc-by-sa-4.0` and that tag is
#   WORTHLESS as evidence — it is BEIR's packaging label. BEIR's own README says so:
#   "we do not vouch for their quality or fairness, or claim that you have license to
#   use the dataset". Proof it lies: `BeIR/msmarco` is tagged `cc-by-sa-4.0` while MS
#   MARCO's own terms say "intended for non-commercial research purposes only".
#   Twelve more BEIR tasks were assessed and REJECTED — TREC-COVID's CORD-19 corpus is
#   a non-transferable "text and data mining only" EULA, NFCorpus is "free to use for
#   academic purposes", ArguAna/Touché sit on idebate.org content. The full
#   considered/rejected table with quoted licence text is .rag-403/beir-slice.md.
#   Do not add a third arm without repeating that provenance work.
#
# Tiering (see .rag-403/multilingual-corpus-plan.md and
# .rag-403/document-corpus-plan.md): Tier A = publishable
# (MIT/Apache/CC-BY/CC0/PD/ODC-BY/CDLA-Permissive), Tier B = local use only, no
# published metric. Tier B arms refuse to download without --accept-noncommercial.
#
# The multilingual arms are pinned by a per-dataset manifest under
# scripts/rag-eval-manifests/<key>.tsv (sha256 · relative path · pinned URL). That
# file, not this script, is the authority: fetching needs no dataset-hub API call,
# and --verify is entirely offline. Re-pin with --refresh-manifest after bumping a
# revision, never by editing a hash by hand.
#
# Usage:
#   ./scripts/fetch-rag-eval-data.sh --licenses          # print licences, download nothing
#   ./scripts/fetch-rag-eval-data.sh --accept-licenses   # fetch everything missing
#   ./scripts/fetch-rag-eval-data.sh --accept-licenses --only qmsum
#   ./scripts/fetch-rag-eval-data.sh --accept-licenses --only miracl
#   ./scripts/fetch-rag-eval-data.sh --accept-licenses --accept-noncommercial --only xlsum
#   ./scripts/fetch-rag-eval-data.sh --verify            # re-check checksums only
#   ./scripts/fetch-rag-eval-data.sh --verify --only miracl
#   ./scripts/fetch-rag-eval-data.sh --accept-licenses --force   # re-download + re-extract
#   ./scripts/fetch-rag-eval-data.sh --refresh-manifest --only mldr   # re-pin from upstream
#
# Environment:
#   RAG_EVAL_DATA_DIR         target root (default /mnt/nas/opentranscribe-benchmarks)
#   RAG_EVAL_ACCEPT_LICENSES  set to 1/true for non-interactive equivalent of --accept-licenses
#   RAG_EVAL_PARALLEL         concurrent downloads per multilingual arm (default 4)
#
# Exit codes: 0 ok · 1 download/checksum failure · 2 misuse · 3 licences not accepted
#
# Idempotent: an archive whose SHA256 already matches is never re-downloaded, and
# a populated extract directory is never re-extracted (use --force to override).

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

DATA_DIR="${RAG_EVAL_DATA_DIR:-/mnt/nas/opentranscribe-benchmarks}"

# --- Dataset manifest --------------------------------------------------------
#
# Checksums recorded 2026-08-12 from the fetch that produced the corpus described
# in $DATA_DIR/README.md. A mismatch is a REAL finding — upstream re-cut the file
# (or the transfer was truncated); do not "fix" it by editing the hash without
# re-reading the source page.
#
# The QMSum entry pins a git COMMIT, not a branch. GitHub's codeload tarball for a
# fixed commit has been byte-stable in practice but is not contractually so; if the
# hash ever moves, confirm the commit is unchanged before touching this value.

# TIER is enforced, not decorative (same rule as the multilingual table below):
#   A = permissive, a metric derived from it MAY be published.
#   B = non-commercial / no-licence-granted. Fetch needs --accept-noncommercial,
#       and NO number derived from it may appear in a published artefact.
DATASETS=(qmsum ami icsi icsi-orig earnings meetingbank elitr elitr-bench)

declare -A META=(
    [qmsum.TIER]="A"
    [qmsum.NAME]="QMSum — query-based multi-domain meeting summarization"
    [qmsum.LICENSE]="MIT License"
    [qmsum.LICENSE_URL]="https://github.com/Yale-LILY/QMSum/blob/main/LICENSE"
    [qmsum.HOMEPAGE]="https://github.com/Yale-LILY/QMSum"
    [qmsum.URL]="https://codeload.github.com/Yale-LILY/QMSum/tar.gz/83d7768c1f2b4dfeb091385d3dc7e239b8e5bb7e"
    [qmsum.SUBDIR]="qmsum"
    [qmsum.ARCHIVE]="QMSum-83d7768c1f2b4dfeb091385d3dc7e239b8e5bb7e.tar.gz"
    [qmsum.SHA256]="b6970687b0f56dbd0a7f66a2ff15c501a3e57e6f60750971466c678cf5b17d7f"
    [qmsum.EXTRACT]="QMSum-83d7768c1f2b4dfeb091385d3dc7e239b8e5bb7e"
    [qmsum.REFERER]=""
    [qmsum.NOTE]="Upstream transcripts re-distribute AMI (Product), ICSI (Academic) and
              Welsh/Canadian parliamentary committee records (Committee). MIT covers
              the QMSum annotations; the underlying corpora keep their own terms
              (AMI: CC BY 4.0; ICSI: CC BY 4.0)."

    [ami.TIER]="A"
    [ami.NAME]="AMI Meeting Corpus — manual (NXT) annotations v1.6.2"
    [ami.LICENSE]="Creative Commons Attribution 4.0 International (CC BY 4.0)"
    [ami.LICENSE_URL]="https://groups.inf.ed.ac.uk/ami/corpus/license.shtml"
    [ami.HOMEPAGE]="https://groups.inf.ed.ac.uk/ami/download/"
    [ami.URL]="https://groups.inf.ed.ac.uk/ami/AMICorpusAnnotations/ami_public_manual_1.6.2.zip"
    [ami.SUBDIR]="ami"
    [ami.ARCHIVE]="ami_public_manual_1.6.2.zip"
    [ami.SHA256]="b56e5babb2496b8795deeeda7e71178d7fbc9963f94276cf2a3f4b56ebbc9f9d"
    [ami.EXTRACT]="ami_public_manual_1.6.2"
    # groups.inf.ed.ac.uk hot-link-protects the annotation archives: a request with
    # no Referer from the download page is answered 403. This is not an auth wall.
    [ami.REFERER]="https://groups.inf.ed.ac.uk/ami/download/"
    [ami.NOTE]="Annotations only — no audio, no video. Attribution is REQUIRED by CC BY 4.0:
              cite Carletta et al., 'The AMI Meeting Corpus: A Pre-Announcement' (MLMI 2005)."

    [icsi.TIER]="A"
    [icsi.NAME]="ICSI Meeting Corpus — NXT annotations + third-party contributions v1.0"
    [icsi.LICENSE]="Creative Commons Attribution 4.0 International (CC BY 4.0)"
    [icsi.LICENSE_URL]="https://groups.inf.ed.ac.uk/ami/icsi/license.shtml"
    [icsi.HOMEPAGE]="https://groups.inf.ed.ac.uk/ami/icsi/download/"
    [icsi.URL]="https://groups.inf.ed.ac.uk/ami/ICSICorpusAnnotations/ICSI_plus_NXT.zip"
    [icsi.SUBDIR]="icsi"
    [icsi.ARCHIVE]="ICSI_plus_NXT.zip"
    [icsi.SHA256]="e4ab79a8b638bdd29121d744c452c680fe23791e406ffe5125e2f5b118923439"
    [icsi.EXTRACT]="ICSI_plus_NXT"
    # Same Edinburgh host as ami, same hot-link protection.
    [icsi.REFERER]="https://groups.inf.ed.ac.uk/ami/icsi/download/"
    [icsi.NOTE]="Annotations only — the ~70 h of audio is NOT fetched. LICENCE.txt inside the
              archive states the corpus AND its annotations are CC BY 4.0, so the
              third-party Contributions/ (topic segmentation, summarization, hotspots)
              are covered too; their READMEs disclaim QUALITY, not licence. Attribution
              REQUIRED: cite Janin et al., 'The ICSI Meeting Corpus' (ICASSP 2003).
              Files are ISO-8859-1 NXT XML, exactly like ami."

    [icsi-orig.TIER]="A"
    [icsi-orig.NAME]="ICSI Meeting Corpus — original MRT-format transcripts + documentation"
    [icsi-orig.LICENSE]="Creative Commons Attribution 4.0 International (CC BY 4.0)"
    [icsi-orig.LICENSE_URL]="https://groups.inf.ed.ac.uk/ami/icsi/license.shtml"
    [icsi-orig.HOMEPAGE]="https://groups.inf.ed.ac.uk/ami/icsi/download/"
    [icsi-orig.URL]="https://groups.inf.ed.ac.uk/ami/ICSICorpusAnnotations/ICSI_original_transcripts.zip"
    [icsi-orig.SUBDIR]="icsi"
    [icsi-orig.ARCHIVE]="ICSI_original_transcripts.zip"
    [icsi-orig.SHA256]="9db98ebdd61a085b4f45f24f46e9c9f6c6f26c3c9cd41643a3b9e74362c85de7"
    [icsi-orig.EXTRACT]="ICSI_original_transcripts"
    [icsi-orig.REFERER]="https://groups.inf.ed.ac.uk/ami/icsi/download/"
    [icsi-orig.NOTE]="4 MB. Same speech as icsi in the simpler single-file MRT format plus the
              corpus documentation. Kept because it is a far cheaper parse than NXT
              when all you need is 'who said what', and it is the tie-breaker when the
              NXT reader and the QMSum turn text disagree."

    [earnings.TIER]="A"
    [earnings.NAME]="Rev.com Earnings-21 + Earnings-22 — earnings-call transcripts"
    [earnings.LICENSE]="Creative Commons Attribution-ShareAlike 4.0 International (CC BY-SA 4.0)"
    [earnings.LICENSE_URL]="https://github.com/revdotcom/speech-datasets/blob/main/earnings21/LICENSE.md"
    [earnings.HOMEPAGE]="https://github.com/revdotcom/speech-datasets"
    [earnings.URL]="https://codeload.github.com/revdotcom/speech-datasets/tar.gz/c05ab6fd8b4b627d123c922a22a39e993dd37635"
    [earnings.SUBDIR]="earnings"
    [earnings.ARCHIVE]="speech-datasets-c05ab6fd8b4b627d123c922a22a39e993dd37635.tar.gz"
    [earnings.SHA256]="d1a63d9cee275c29582f8988abee246453ee632c85ff2552c4019b2eccc1944a"
    [earnings.EXTRACT]="speech-datasets-c05ab6fd8b4b627d123c922a22a39e993dd37635"
    [earnings.REFERER]=""
    [earnings.NOTE]="CC BY-SA 4.0 covers THE TRANSCRIPTS (LICENSE.md is scoped to 'the transcripts
              and associated text files'), which is all this fetches. ShareAlike:
              a redistributed derivative of the transcript text must carry the same
              licence — fine for reporting metrics, relevant if we ever ship a
              processed copy. The tarball is pinned to a COMMIT because main moves.
              media/ (audio), longform_reconstitution/ and coraal-multi/ are excluded
              at extract time; they are ~590 MB the harness never reads."

    [meetingbank.TIER]="B"
    [meetingbank.NAME]="MeetingBank — 1,366 US city-council meetings (ACL 2023)"
    [meetingbank.LICENSE]="CC BY-NC-ND 4.0 — NON-COMMERCIAL, NO DERIVATIVES"
    [meetingbank.LICENSE_URL]="https://meetingbank.github.io/license/"
    [meetingbank.HOMEPAGE]="https://zenodo.org/records/7989108"
    [meetingbank.URL]="https://zenodo.org/records/7989108/files/MeetingBank.zip?download=1"
    [meetingbank.SUBDIR]="meetingbank"
    [meetingbank.ARCHIVE]="MeetingBank.zip"
    [meetingbank.SHA256]="36a7c250b0a8895c18eba2f2d89cf1d12f32017626ce868450995dfaef43c191"
    [meetingbank.EXTRACT]="MeetingBank"
    [meetingbank.REFERER]=""
    [meetingbank.NOTE]="TIER B AND ITS LICENCE IS MIS-ADVERTISED IN THREE PLACES. Zenodo's metadata
              field says 'cc-by-4.0'; the HuggingFace mirrors say 'cc-by-nc-sa-4.0';
              LICENSE.txt INSIDE this very archive and the authors' own project page
              both say Attribution-NonCommercial-NoDerivatives 4.0. The file in the
              package wins: treat it as NC-ND, local use only, no published metric.
              Despite the 'Audio&Transcripts' directory name the archive holds ZERO
              media — 1,366 JSON ASR transcripts with word-level timings and diarized
              speaker indices. 'uploaded/' is a byte-identical duplicate of Metadata/
              (an authors' packaging slip) and is excluded at extract time."

    [elitr.TIER]="B"
    [elitr.NAME]="ELITR Minuting Corpus — meetings with human minutes and span alignments"
    [elitr.LICENSE]="CC BY-NC-SA 4.0 — NON-COMMERCIAL, ShareAlike"
    [elitr.LICENSE_URL]="https://lindat.mff.cuni.cz/repository/xmlui/handle/11234/1-4692"
    [elitr.HOMEPAGE]="https://ufal.mff.cuni.cz/elitr-minuting-corpus"
    # The handle/bitstream URL serves a JavaScript interstitial, not the file. This is
    # the DSpace REST content endpoint, which returns the bytes directly.
    [elitr.URL]="https://lindat.mff.cuni.cz/repository/server/api/core/bitstreams/76466f35-1bac-47bf-958b-c235b1fb4966/content"
    [elitr.SUBDIR]="elitr"
    [elitr.ARCHIVE]="ELITR-minuting-corpus.zip"
    [elitr.SHA256]="34868d54a983b46841c8a8c14153d48c913f40bd92e37b23a85f628be737cbe1"
    [elitr.EXTRACT]="ELITR-minuting-corpus"
    [elitr.REFERER]=""
    [elitr.NOTE]="TIER B (CC BY-NC-SA 4.0, stated on the LINDAT item page). 120 EN + 59 CS
              meetings, 403 human minutes, and — the reason it is here — MANUAL
              ALIGNMENTS between minute items and transcript spans, which is a
              qrels-shaped signal no other Tier B set provides. Already pseudonymised
              to [PERSON5]/(PERSON8), so it doubles as a redaction fixture. Recordings
              were withheld upstream for privacy; there is no audio to fetch."

    [elitr-bench.TIER]="B"
    [elitr-bench.NAME]="ELITR-Bench — 271 human QA pairs over 18 ELITR-minuting transcripts"
    [elitr-bench.LICENSE]="SPLIT: QA layer CC BY 4.0 (LICENSE-DATA.txt in-repo); transcripts inherit ELITR-minuting's CC BY-NC-SA 4.0"
    [elitr-bench.LICENSE_URL]="https://github.com/utter-project/ELITR-Bench/blob/main/LICENSE-DATA.txt"
    [elitr-bench.HOMEPAGE]="https://github.com/utter-project/ELITR-Bench"
    [elitr-bench.URL]="https://codeload.github.com/utter-project/ELITR-Bench/tar.gz/83649521b016e78b6293ceaf2585ea96f2ed9162"
    [elitr-bench.SUBDIR]="elitr-bench"
    [elitr-bench.ARCHIVE]="ELITR-Bench-8364952.tar.gz"
    [elitr-bench.SHA256]="e93aeece8945934478718bc2b4c9c0ff233bcd62800fb4e749ec2bad343c9d98"
    [elitr-bench.EXTRACT]="ELITR-Bench-83649521b016e78b6293ceaf2585ea96f2ed9162"
    [elitr-bench.REFERER]=""
    [elitr-bench.NOTE]="TIER B via the TRANSCRIPTS, not the QA layer (issue #521): data.zip in
              this repo (password 'utter', an anti-contamination guard documented in its
              README) holds only questions/answers/metadata under CC BY 4.0. The 18
              transcripts are extracted FROM the elitr dataset above (CC BY-NC-SA) by
              'python -m preparation.extract_transcripts' run at the repo root beside an
              unzipped ELITR-minuting-corpus. Staged layout on the NAS: data/ = the 4 QA
              JSONs, transcripts/ = the 18 extracted .txt files (meeting_en_dev_006 is
              unattributed — no (PERSONn) markers in its source variant; measured).
              96 of the 271 questions are the 'who' category — the speaker-attribution
              axis nothing else in this table tests with human-written QA."
)

# --- Multilingual / cross-lingual manifest (issue #403) -----------------------
#
# A second dataset table with a different shape: these are multi-file corpora, not
# single archives, so each has a PINNED MANIFEST at scripts/rag-eval-manifests/<key>.tsv
# listing every file as "<sha256>\t<relative path>\t<pinned URL>". SOURCE below records
# where that manifest came from and is only consulted by --refresh-manifest.
#
# SOURCE forms:
#   hf:<repo>@<revision>   dataset repo at a pinned commit; INCLUDE/EXCLUDE are
#                          ERE filters over the repo-relative path. The revision is
#                          pinned for the same reason QMSum pins a commit: "main"
#                          moves, and a corpus that changes underneath a published
#                          nDCG number invalidates it.
#   url:<base>             plain HTTP; FILES is a whitespace-separated list appended
#                          to <base>. No API, no hub.
#
# TIER is enforced, not decorative: tier B needs --accept-noncommercial.

ML_DATASETS=(miracl miracl-corpus ciral ciral-corpus mrtydi mrtydi-corpus
             mldr lareqa multi-eup voxpopuli-asr fleurs qrecc xlsum)

declare -A MLMETA=(
    [miracl.NAME]="MIRACL v1.0 — topics and qrels (18 languages)"
    [miracl.LICENSE]="Apache License 2.0"
    [miracl.LICENSE_URL]="https://github.com/project-miracl/miracl/blob/main/LICENSE"
    [miracl.HOMEPAGE]="https://huggingface.co/datasets/miracl/miracl"
    [miracl.TIER]="A"
    [miracl.SOURCE]="hf:miracl/miracl@5be20db9509754dadad47689368639fcec739c00"
    [miracl.INCLUDE]="^miracl-v1\.0-"
    [miracl.EXCLUDE]="^\$"
    [miracl.SUBDIR]="multilingual/miracl"
    [miracl.NOTE]="THE anchor for every non-English retrieval claim. Human graded relevance
              judgements in TREC format, and the qrels carry explicit NEGATIVE (0)
              rows as well as positives — the judged pool is known, so unjudged is
              distinguishable from judged-irrelevant. de and yo are dev-only
              ('surprise' languages, no train split)."

    [miracl-corpus.NAME]="MIRACL v1.0 — Wikipedia passage corpus (18 languages)"
    [miracl-corpus.LICENSE]="Apache License 2.0 (annotations); underlying text is Wikipedia, CC BY-SA"
    [miracl-corpus.LICENSE_URL]="https://huggingface.co/datasets/miracl/miracl-corpus"
    [miracl-corpus.HOMEPAGE]="https://huggingface.co/datasets/miracl/miracl-corpus"
    [miracl-corpus.TIER]="A"
    [miracl-corpus.SOURCE]="hf:miracl/miracl-corpus@d921ec7e349ce0d28daf30b2da9da5ee698bef0d"
    [miracl-corpus.INCLUDE]="^miracl-corpus-v1\.0-"
    [miracl-corpus.EXCLUDE]="^\$"
    [miracl-corpus.SUBDIR]="multilingual/miracl-corpus"
    [miracl-corpus.NOTE]="~15 GB and DELIBERATELY LEFT GZIPPED — decompressing all 18 languages
              costs roughly 50 GB for no benefit, since the adapter streams .jsonl.gz.
              Do not add an extract step. Apache-2.0 covers the segmentation; the
              passage text is Wikipedia and keeps CC BY-SA, so attribute on publication."

    [ciral.NAME]="CIRAL v1.0 — topics and qrels (Hausa, Somali, Swahili, Yoruba)"
    [ciral.LICENSE]="Apache License 2.0"
    [ciral.LICENSE_URL]="https://huggingface.co/datasets/CIRAL/ciral"
    [ciral.HOMEPAGE]="https://huggingface.co/datasets/CIRAL/ciral"
    [ciral.TIER]="A"
    [ciral.SOURCE]="hf:CIRAL/ciral@b70d52ca77d3c462db447c65377aee0406512570"
    [ciral.INCLUDE]="^ciral-"
    [ciral.EXCLUDE]="^\$"
    [ciral.SUBDIR]="multilingual/ciral"
    [ciral.NOTE]="CROSS-lingual: the queries are ENGLISH, the passages are African-language.
              The only Tier A source here that scores Hausa and Somali at all."

    [ciral-corpus.NAME]="CIRAL v1.0 — passage corpus + English machine translations"
    [ciral-corpus.LICENSE]="Apache License 2.0"
    [ciral-corpus.LICENSE_URL]="https://huggingface.co/datasets/CIRAL/ciral-corpus"
    [ciral-corpus.HOMEPAGE]="https://huggingface.co/datasets/CIRAL/ciral-corpus"
    [ciral-corpus.TIER]="A"
    [ciral-corpus.SOURCE]="hf:CIRAL/ciral-corpus@81644d6e18427ab79a36bb31141c9281239ba40c"
    [ciral-corpus.INCLUDE]="passages-v1\.0/"
    [ciral-corpus.EXCLUDE]="^\$"
    [ciral-corpus.SUBDIR]="multilingual/ciral-corpus"
    [ciral-corpus.NOTE]="Both passages-v1.0/ (native) and translated-passages-v1.0/ (English MT) are
              fetched: the MT copy is the translate-then-retrieve control the paper
              needs to separate 'the retriever is multilingual' from 'the translation
              was good'."

    [mrtydi.NAME]="Mr. TyDi v1.1 — topics, qrels and train/dev/test (11 languages)"
    [mrtydi.LICENSE]="Apache License 2.0"
    [mrtydi.LICENSE_URL]="https://huggingface.co/datasets/castorini/mr-tydi"
    [mrtydi.HOMEPAGE]="https://huggingface.co/datasets/castorini/mr-tydi"
    [mrtydi.TIER]="A"
    [mrtydi.SOURCE]="hf:castorini/mr-tydi@1d43c80218d06d0ef80f5b172ccabd848b948bc1"
    [mrtydi.INCLUDE]="^mrtydi-v1\.1-"
    [mrtydi.EXCLUDE]="^\$"
    [mrtydi.SUBDIR]="multilingual/mrtydi"
    [mrtydi.NOTE]="Adds NO new language over MIRACL — that is the point. It is an independently
              built, independently judged benchmark over the same languages, so it
              tells you whether a ranking is a property of the retriever or of
              MIRACL's pooling. Use ir-format-data/ (TREC topics + qrels)."

    [mrtydi-corpus.NAME]="Mr. TyDi v1.1 — Wikipedia corpus (English EXCLUDED)"
    [mrtydi-corpus.LICENSE]="Apache License 2.0 (annotations); underlying text is Wikipedia, CC BY-SA"
    [mrtydi-corpus.LICENSE_URL]="https://huggingface.co/datasets/castorini/mr-tydi-corpus"
    [mrtydi-corpus.HOMEPAGE]="https://huggingface.co/datasets/castorini/mr-tydi-corpus"
    [mrtydi-corpus.TIER]="A"
    [mrtydi-corpus.SOURCE]="hf:castorini/mr-tydi-corpus@3a3aa212bbe94a8cc0dc858710a3dad49d532054"
    [mrtydi-corpus.INCLUDE]="^mrtydi-v1\.1-"
    [mrtydi-corpus.EXCLUDE]="english"
    [mrtydi-corpus.SUBDIR]="multilingual/mrtydi-corpus"
    [mrtydi-corpus.NOTE]="The English corpus (4.7 GB of the 8.4 GB) is deliberately NOT fetched:
              English is already scored by MIRACL-en and by the meeting corpora, and
              Mr. TyDi's value here is the non-English half. Adding 'english' back to
              INCLUDE is a conscious +4.7 GB, not a bug fix."

    [mldr.NAME]="MLDR — multilingual LONG-document retrieval (13 languages)"
    [mldr.LICENSE]="MIT License"
    [mldr.LICENSE_URL]="https://huggingface.co/datasets/Shitao/MLDR"
    [mldr.HOMEPAGE]="https://huggingface.co/datasets/Shitao/MLDR"
    [mldr.TIER]="A"
    [mldr.SOURCE]="hf:Shitao/MLDR@d67138e705d963e346253a80e59676ddb418810a"
    [mldr.INCLUDE]="^mldr-v1\.0-"
    [mldr.EXCLUDE]="train\.jsonl\.gz\$"
    [mldr.SUBDIR]="multilingual/mldr"
    [mldr.NOTE]="ITS QUERIES ARE SYNTHETIC — generated by GPT-3.5 from the passage they are
              then judged relevant to. One positive per query, no pooling, no human in
              the loop. That is a weaker judgement than MIRACL's and any number from it
              must be labelled as such. Kept because it is the only multilingual set
              whose documents are transcript-length (avg 4.7k tokens). train.jsonl.gz
              is excluded: 6.2 GB of mined hard negatives no evaluation reads."

    [lareqa.NAME]="LAReQA / XQuAD-R — 11-way parallel sentence retrieval"
    [lareqa.LICENSE]="Creative Commons Attribution-ShareAlike 4.0 International (CC BY-SA 4.0)"
    [lareqa.LICENSE_URL]="https://github.com/google-research-datasets/lareqa/blob/master/LICENSE"
    [lareqa.HOMEPAGE]="https://github.com/google-research-datasets/lareqa"
    [lareqa.TIER]="A"
    [lareqa.SOURCE]="url:https://raw.githubusercontent.com/google-research-datasets/lareqa/9bc8c7fb6dd8d01d72a05a93c2cb96882b0d299c/"
    [lareqa.FILES]="LICENSE README.md xquad-r/ar.json xquad-r/de.json xquad-r/el.json
                    xquad-r/en.json xquad-r/es.json xquad-r/hi.json xquad-r/ru.json
                    xquad-r/th.json xquad-r/tr.json xquad-r/vi.json xquad-r/zh.json"
    [lareqa.SUBDIR]="multilingual/lareqa"
    [lareqa.NOTE]="18 MB that buys Greek, Turkish and Vietnamese — the cheapest language
              coverage on this list. Retrieval unit is a SENTENCE, not a passage, so it
              measures embedding alignment rather than end-to-end chunk retrieval;
              report it as a separate task, never pooled with MIRACL nDCG."

    [multi-eup.NAME]="Multi-EuP — European Parliament debates, 24 EU languages"
    [multi-eup.LICENSE]="Apache License 2.0 (dataset); underlying EP data CC BY 4.0"
    [multi-eup.LICENSE_URL]="https://huggingface.co/datasets/unimelb-nlp/Multi-EuP"
    [multi-eup.HOMEPAGE]="https://aclanthology.org/2023.mrl-1.21"
    [multi-eup.TIER]="A"
    [multi-eup.SOURCE]="hf:unimelb-nlp/Multi-EuP@61b5e3b5e3ee730377cf8481ce5a741a928a087f"
    [multi-eup.INCLUDE]="\.csv\$"
    [multi-eup.EXCLUDE]="^\$"
    [multi-eup.SUBDIR]="multilingual/multi-eup"
    [multi-eup.NOTE]="Its 'qrels' are STRUCTURAL, not human: the query is a speech's own title and
              the single relevant document is that speech. Real multilingual speech-derived
              text across 24 EU languages (the widest here), but treat retrieval numbers
              from it as a sanity check, not evidence."

    [voxpopuli-asr.NAME]="VoxPopuli — ASR transcript annotations, 16 languages (NO AUDIO)"
    [voxpopuli-asr.LICENSE]="CC0 1.0 Universal (public domain dedication)"
    [voxpopuli-asr.LICENSE_URL]="https://github.com/facebookresearch/voxpopuli#license"
    [voxpopuli-asr.HOMEPAGE]="https://github.com/facebookresearch/voxpopuli"
    [voxpopuli-asr.TIER]="A"
    [voxpopuli-asr.SOURCE]="url:https://dl.fbaipublicfiles.com/voxpopuli/annotations/asr/"
    [voxpopuli-asr.FILES]="asr_en.tsv.gz asr_de.tsv.gz asr_fr.tsv.gz asr_es.tsv.gz asr_pl.tsv.gz
                           asr_it.tsv.gz asr_ro.tsv.gz asr_hu.tsv.gz asr_cs.tsv.gz asr_nl.tsv.gz
                           asr_fi.tsv.gz asr_hr.tsv.gz asr_sk.tsv.gz asr_sl.tsv.gz asr_et.tsv.gz
                           asr_lt.tsv.gz"
    [voxpopuli-asr.SUBDIR]="multilingual/voxpopuli-asr"
    [voxpopuli-asr.NOTE]="NO QUERIES, NO QRELS — this scores nothing. It is the only genuinely
              multilingual SPEECH transcript source here (real parliamentary speech,
              speaker ids, segment timings), so it is what makes multilingual ingest
              realistic. Transcripts only: the audio is hundreds of GB and unused."

    [fleurs.NAME]="FLEURS — read-speech transcripts, 101 locales (NO AUDIO)"
    [fleurs.LICENSE]="Creative Commons Attribution 4.0 International (CC BY 4.0)"
    [fleurs.LICENSE_URL]="https://huggingface.co/datasets/google/fleurs"
    [fleurs.HOMEPAGE]="https://huggingface.co/datasets/google/fleurs"
    [fleurs.TIER]="A"
    [fleurs.SOURCE]="hf:google/fleurs@70bb2e84b976b7e960aa89f1c648e09c59f894dd"
    [fleurs.INCLUDE]="^data/[a-z_]+/[a-z]+\.tsv\$"
    [fleurs.EXCLUDE]="^\$"
    [fleurs.SUBDIR]="multilingual/fleurs"
    [fleurs.NOTE]="NO QUERIES, NO QRELS. Isolated read-aloud sentences, not discourse — it
              cannot stand in for conversational text. Its one job is breadth: 101
              locales of reference transcript for language-detection and
              tokenisation/chunking sanity checks. The audio tarballs are excluded."

    [qrecc.NAME]="QReCC — open-domain CONVERSATIONAL question rewriting and retrieval"
    [qrecc.LICENSE]="Creative Commons Attribution 3.0 (CC BY 3.0)"
    [qrecc.LICENSE_URL]="https://huggingface.co/datasets/svakulenk0/qrecc"
    [qrecc.HOMEPAGE]="https://github.com/apple/ml-qrecc"
    [qrecc.TIER]="A"
    [qrecc.SOURCE]="hf:svakulenk0/qrecc@433e142816781b4cb97022bc2bd245e138a82140"
    [qrecc.INCLUDE]="\.json\$"
    [qrecc.EXCLUDE]="^\$"
    [qrecc.SUBDIR]="multilingual/qrecc"
    [qrecc.NOTE]="ENGLISH ONLY — it is here for the multi-turn axis, not the language axis.
              Each turn carries the context-dependent question, the human REWRITE, and
              the gold answer URL, which is exactly the follow-up-question behaviour the
              chat pipeline has to get right. Its 54M-passage collection is NOT fetched
              (~25 GB); scoring full retrieval on it needs that collection first."

    [xlsum.NAME]="XL-Sum — abstractive news summarisation, 45 languages"
    [xlsum.LICENSE]="CC BY-NC-SA 4.0 — NON-COMMERCIAL, research use only"
    [xlsum.LICENSE_URL]="https://huggingface.co/datasets/csebuetnlp/xlsum"
    [xlsum.HOMEPAGE]="https://github.com/csebuetnlp/xl-sum"
    [xlsum.TIER]="B"
    [xlsum.SOURCE]="hf:csebuetnlp/xlsum@30fece425f9a3866e04321773ca7a80056d55ca6"
    [xlsum.INCLUDE]="\.tar\.bz2\$"
    [xlsum.EXCLUDE]="^\$"
    [xlsum.SUBDIR]="multilingual/xlsum"
    [xlsum.NOTE]="TIER B — the card states 'restricted to only non-commercial research
              purposes'. Use it locally to exercise the summarise query class in 45
              languages; NEVER put a number derived from it in a published artefact."

    # --- Retrieval benchmark suites (RB_DATASETS) ----------------------------
    # Same multi-file machinery, different purpose: these exist so the paper's
    # QMSum nDCG@10 can be placed next to a PUBLISHED QMSum nDCG@10.
    [locov1.NAME]="LoCoV1 — long-context retrieval benchmark (qmsum + summ_screen_fd)"
    [locov1.LICENSE]="Apache License 2.0"
    [locov1.LICENSE_URL]="https://huggingface.co/datasets/hazyresearch/LoCoV1-Queries"
    [locov1.HOMEPAGE]="https://arxiv.org/abs/2402.07440"
    [locov1.TIER]="A"
    [locov1.SOURCE]="hf:hazyresearch/LoCoV1-Queries@8b55e17ef3ee008e38f9e0829612a11afb30cd67"
    [locov1.INCLUDE]="qmsum|summ_screen_fd"
    [locov1.EXCLUDE]="^\$"
    [locov1.SUBDIR]="retrieval-benchmarks/locov1"
    [locov1.NOTE]="Only the two CONVERSATIONAL subsets are pinned; LoCo's legal/gov/stackoverflow
              subsets are the document arm's scope, not this one. Shape: queries carry
              qid/query/answer_pids, documents carry pid/passage — so the qrels are
              BINARY and DOCUMENT-level (one whole meeting per query), not chunk-level.
              qmsum here is exactly QMSum's 35-meeting test split with 272 queries.
              Its manifest is hand-pinned from BOTH the -Queries and -Documents repos,
              so --refresh-manifest would only see half of it; re-pin by hand."

    [longembed.NAME]="LongEmbed — QMSum long-context retrieval subset"
    [longembed.LICENSE]="NO LICENCE STATED by the publisher (neither HF card nor GitHub repo)"
    [longembed.LICENSE_URL]="https://huggingface.co/datasets/dwzhu/LongEmbed"
    [longembed.HOMEPAGE]="https://arxiv.org/abs/2404.12096"
    [longembed.TIER]="B"
    [longembed.SOURCE]="hf:dwzhu/LongEmbed@10039a580487dacecf79db69166e17ace3ede392"
    [longembed.INCLUDE]="^qmsum/"
    [longembed.EXCLUDE]="^\$"
    [longembed.SUBDIR]="retrieval-benchmarks/longembed"
    [longembed.NOTE]="TIER B FOR AN UNUSUAL REASON: not non-commercial, but UNLICENSED. No licence
              appears on the HF card or the GitHub repo, and absent a grant the default
              is all-rights-reserved. Its content derives from QMSum (MIT), so the TASK
              is reproducible — rebuild it from our own QMSum copy for anything
              published and use these files only to prove the rebuild matches.
              197 docs / 1,527 queries / 1,527 qrels rows, one gold doc per query."
)

# Registered in MLMETA above and fetched by the same multi-file loop, but kept in a
# separate array so each agent's generated MANIFEST.tsv stays its own file.
RB_DATASETS=(locov1 longembed)

# --- General-IR slice: BEIR (issue #403) --------------------------------------
#
# Land under <root>/beir/. Fourth array, fourth generated manifest, same reason as
# the previous three: separate agents, no shared generated file, no merge conflict.
# Mechanically these are `url:` single-zip arms,
# so they ride the shared multi-file loop; only the extract pass differs (see
# beir_extract below — a BEIR zip already contains its own <task>/ top level, so it
# unpacks into the PARENT of SUBDIR, not into SUBDIR).
#
# ⚠️ THE LICENCE HERE IS THE WHOLE POINT. Do not add a task on the strength of the
# `BeIR/*` HuggingFace tag (uniformly `cc-by-sa-4.0` across all 19 tasks) OR of the
# BEIR paper's Appendix E. Both are demonstrably wrong, in opposite directions:
#
#   • The HF tag INVENTS grants. `BeIR/climate-fever` is tagged cc-by-sa-4.0 while
#     Climate-FEVER's authors never granted any licence anywhere (its own HF card
#     reads `license: unknown`). `BeIR/msmarco` is tagged cc-by-sa-4.0 while
#     Microsoft says the data is provided "without extending any license or other
#     intellectual property rights".
#   • Appendix E goes STALE. It records SciFact as CC BY-NC 2.0 — true when the paper
#     was written, false since 2023-09-28, when AI2 relicensed it in commit
#     7e07702341 (see the scifact NOTE). Trusting the paper would have wrongly
#     rejected a Tier A task.
#
# Twelve other tasks were assessed and rejected. Quoted licence text, URLs, sizes,
# counts and the published nDCG@10 baselines we compare against: .rag-403/beir-slice.md.
BEIR_DATASETS=(beir-hotpotqa beir-scifact)

MLMETA+=(
    [beir-hotpotqa.TIER]="A"
    [beir-hotpotqa.NAME]="BEIR / HotpotQA — multi-hop QA over 5.23 M Wikipedia paragraphs"
    [beir-hotpotqa.LICENSE]="Creative Commons Attribution-ShareAlike 4.0 International (CC BY-SA 4.0)"
    [beir-hotpotqa.LICENSE_URL]="https://hotpotqa.github.io/"
    [beir-hotpotqa.HOMEPAGE]="https://hotpotqa.github.io/"
    [beir-hotpotqa.SOURCE]="url:https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/"
    [beir-hotpotqa.FILES]="hotpotqa.zip"
    [beir-hotpotqa.SUBDIR]="beir/hotpotqa"
    [beir-hotpotqa.EXTRACTS]="hotpotqa.zip:corpus.jsonl"
    [beir-hotpotqa.NOTE]="THE SCALE ARM. 5,233,329 documents / 7,405 test queries / 2.0 gold docs per
              query — the only Tier A task in this lane big enough to show the hybrid
              BM25+kNN+RRF stack still works at five million documents, and multi-hop
              queries are the closest public analogue of #403's multi-file class.
              Licence traced to the AUTHORS, not to BEIR: hotpotqa.github.io states
              'HotpotQA is distributed under a CC BY-SA 4.0 License' and, separately,
              that the processed Wikipedia BEIR uses as the corpus is released 'also
              under a CC BY-SA 4.0 License' — so both halves of what we download are
              covered. Trap: the github.com/hotpotqa/hotpot repo is Apache-2.0, which
              is the CODE licence; an earlier note in .rag-403/eval-corpus-plan.md
              recorded HotpotQA as Apache-2.0 for exactly that reason and was wrong.
              SHARE-ALIKE APPLIES: attribute Yang et al. (EMNLP 2018) and Wikipedia.
              Published BM25 nDCG@10 = 0.603 (BEIR Table 2). UKP-published MD5 for the
              zip is f412724f78b0d91183a0e86805e16114."

    [beir-scifact.TIER]="A"
    [beir-scifact.NAME]="BEIR / SciFact — scientific claim verification over 5,183 abstracts"
    [beir-scifact.LICENSE]="CC BY 4.0 (claims + evidence annotations) / ODC-By 1.0 (corpus abstracts, via S2ORC)"
    [beir-scifact.LICENSE_URL]="https://github.com/allenai/scifact/blob/master/LICENSE.md"
    [beir-scifact.HOMEPAGE]="https://github.com/allenai/scifact"
    [beir-scifact.SOURCE]="url:https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/"
    [beir-scifact.FILES]="scifact.zip"
    [beir-scifact.SUBDIR]="beir/scifact"
    [beir-scifact.EXTRACTS]="scifact.zip:corpus.jsonl"
    [beir-scifact.NOTE]="THE LONG-DOCUMENT ARM, and the cleanest licence in BEIR: allenai/scifact's
              LICENSE.md grants each component separately — claims and evidence
              annotations CC BY 4.0, corpus abstracts ODC-By 1.0 as part of S2ORC,
              code Apache-2.0. Both data grants are Tier A under this repo's tiering.
              ⚠️ BEIR's paper (Appendix E) says 'SciFact: Provided under the CC BY-NC
              2.0 license'. That was TRUE at publication and is now STALE: AI2
              relicensed on 2023-09-28 in commits 7e07702341 (CC BY-NC 2.0 → CC BY 4.0
              + ODC-By) and a5254a1bf1 (typo 'ODB-By' → 'ODC-By'). The current grant
              from the copyright holder governs; do not 'correct' this back to NC on
              the strength of the paper. At 213.63 avg words per document it is the
              third-longest public BEIR corpus and the closest of any Tier A task to
              our transcript chunk size. Only 300 test queries at 1.1 gold docs each,
              so the confidence interval is wide — report it as a sanity check, never
              as a precision claim. Published BM25 nDCG@10 = 0.665 (BEIR Table 2).
              UKP-published MD5 for the zip is 5f7d1de60b170fc8027bb7898e2efca1."
)

# curl needs a browser-ish UA for the Edinburgh host; the default curl UA is refused.
UA='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'

# --- Arguments ---------------------------------------------------------------
ACCEPT=false
VERIFY_ONLY=false
LICENSES_ONLY=false
FORCE=false
ONLY=""
ACCEPT_NC=false
REFRESH_MANIFEST=false
MANIFEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/rag-eval-manifests"
PARALLEL="${RAG_EVAL_PARALLEL:-4}"

case "${RAG_EVAL_ACCEPT_LICENSES:-}" in
    1|true|TRUE|yes) ACCEPT=true ;;
esac

while [[ $# -gt 0 ]]; do
    case "$1" in
        --accept-licenses) ACCEPT=true ;;
        --verify) VERIFY_ONLY=true ;;
        --licenses) LICENSES_ONLY=true ;;
        --force) FORCE=true ;;
        # Separate from --accept-licenses on purpose: accepting a permissive licence
        # and accepting "no published number may come from this" are different
        # decisions, and conflating them is how a Tier B figure reaches a paper.
        --accept-noncommercial) ACCEPT_NC=true ;;
        --refresh-manifest) REFRESH_MANIFEST=true ;;
        --only)
            [[ $# -ge 2 ]] || { echo -e "${RED}--only needs a dataset key${NC}"; exit 2; }
            ONLY="$2"; shift ;;
        # Header block only — stop at the first non-comment line, so the
        # in-body comments don't end up in --help.
        -h|--help) awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"; exit 0 ;;
        *) echo -e "${RED}Unknown option: $1${NC}"; exit 2 ;;
    esac
    shift
done

if [[ -n "$ONLY" ]]; then
    known=false
    for k in "${DATASETS[@]}" "${ML_DATASETS[@]}" "${RB_DATASETS[@]}" "${BEIR_DATASETS[@]}"; do
        [[ "$k" == "$ONLY" ]] && known=true
    done
    if ! $known; then
        echo -e "${RED}Unknown dataset '${ONLY}' — known keys: ${DATASETS[*]} ${ML_DATASETS[*]} ${RB_DATASETS[*]} ${BEIR_DATASETS[*]}${NC}"
        exit 2
    fi
fi

selected() { [[ -z "$ONLY" || "$ONLY" == "$1" ]]; }
field() { echo "${META[$1.$2]}"; }
# A missing metadata field used to abort under `set -u` with only
# "MLMETA[$1.$2]: unbound variable" — no key, no field name, printed mid-run from
# whichever of the four manifest writers touched it. Name the gap instead: the
# writers legitimately probe optional fields, so an absent one is empty, not fatal.
mlfield() {
    if [[ -v "MLMETA[$1.$2]" ]]; then
        echo "${MLMETA[$1.$2]}"
    else
        [[ -n "${RAG_EVAL_DEBUG_META:-}" ]] && echo "meta: $1 has no $2" >&2
        echo ""
    fi
}

# True when --only names a multilingual key. Used to leave the shared top-level
# MANIFEST.tsv alone when this run only touched multilingual/ — that file is
# generated from the DATASETS table and is not this block's to rewrite.
ml_only() {
    [[ -z "$ONLY" ]] && return 1
    for k in "${ML_DATASETS[@]}"; do [[ "$k" == "$ONLY" ]] && return 0; done
    return 1
}
ml_any_selected() {
    for k in "${ML_DATASETS[@]}"; do selected "$k" && return 0; done
    return 1
}

# Same idea for the retrieval-benchmark group: --only locov1 must not rewrite the
# top-level MANIFEST.tsv, which describes the archive corpora.
rb_only() {
    [[ -z "$ONLY" ]] && return 1
    for k in "${RB_DATASETS[@]}"; do [[ "$k" == "$ONLY" ]] && return 0; done
    return 1
}
rb_any_selected() {
    for k in "${RB_DATASETS[@]}"; do selected "$k" && return 0; done
    return 1
}

# ...and for the BEIR general-IR slice (issue #403). Fourth time, same reason:
# --only beir-scifact must not rewrite a manifest it did not touch.
beir_only() {
    [[ -z "$ONLY" ]] && return 1
    for k in "${BEIR_DATASETS[@]}"; do [[ "$k" == "$ONLY" ]] && return 0; done
    return 1
}
beir_any_selected() {
    for k in "${BEIR_DATASETS[@]}"; do selected "$k" && return 0; done
    return 1
}

# Tier gate shared by both tables. Tier B is fetchable, but only deliberately.
tier_blocked() {
    # tier_blocked <tier> -> 0 when this run must NOT fetch it
    [[ "$1" != "A" ]] && ! $ACCEPT_NC && ! $VERIFY_ONLY
}

# --- Licence guard -----------------------------------------------------------
print_licenses() {
    echo -e "${BLUE}================================================================${NC}"
    echo -e "${BLUE}  RAG eval corpora — licences you are accepting${NC}"
    echo -e "${BLUE}================================================================${NC}"
    for key in "${DATASETS[@]}"; do
        selected "$key" || continue
        tier="$(field "$key" TIER)"
        if [[ "$tier" == "A" ]]; then tcol="${GREEN}Tier A — publishable${NC}"
        else tcol="${RED}Tier B — LOCAL USE ONLY, never publish a derived metric${NC}"; fi
        echo -e "${YELLOW}[$key]${NC} $(field "$key" NAME)"
        echo -e "    Tier    : $tcol"
        echo -e "    Licence : $(field "$key" LICENSE)"
        echo -e "    Terms   : $(field "$key" LICENSE_URL)"
        echo -e "    Source  : $(field "$key" URL)"
        echo -e "    Note    : $(field "$key" NOTE)"
        echo ""
    done
    for key in "${ML_DATASETS[@]}" "${RB_DATASETS[@]}" "${BEIR_DATASETS[@]}"; do
        selected "$key" || continue
        tier="$(mlfield "$key" TIER)"
        if [[ "$tier" == "A" ]]; then tcol="${GREEN}Tier A — publishable${NC}"
        else tcol="${RED}Tier B — LOCAL USE ONLY, never publish a derived metric${NC}"; fi
        echo -e "${YELLOW}[$key]${NC} $(mlfield "$key" NAME)"
        echo -e "    Tier    : $tcol"
        echo -e "    Licence : $(mlfield "$key" LICENSE)"
        echo -e "    Terms   : $(mlfield "$key" LICENSE_URL)"
        echo -e "    Source  : $(mlfield "$key" SOURCE)"
        echo -e "    Note    : $(mlfield "$key" NOTE)"
        echo ""
    done
    echo -e "${YELLOW}All of these are third-party data. They live under ${DATA_DIR}"
    echo -e "and must NEVER be committed to this repository.${NC}"
    echo ""
}

if $LICENSES_ONLY; then
    print_licenses
    exit 0
fi

if ! $VERIFY_ONLY && ! $ACCEPT; then
    print_licenses
    echo -e "${RED}Refusing to download: licences not accepted.${NC}"
    echo -e "Re-run with ${YELLOW}--accept-licenses${NC} (or export"
    echo -e "${YELLOW}RAG_EVAL_ACCEPT_LICENSES=1${NC}) to confirm you accept the terms above."
    exit 3
fi

# --- Preconditions -----------------------------------------------------------
for tool in curl sha256sum unzip tar; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo -e "${RED}Required tool not found: ${tool}${NC}"; exit 1; }
done

if [[ ! -d "$DATA_DIR" ]]; then
    if $VERIFY_ONLY; then
        echo -e "${RED}Data dir does not exist: ${DATA_DIR}${NC}"
        exit 1
    fi
    echo -e "${BLUE}Creating ${DATA_DIR}${NC}"
    mkdir -p "$DATA_DIR"
fi

# --- Helpers -----------------------------------------------------------------
sha_of() { sha256sum "$1" | cut -d' ' -f1; }

FAILURES=()

verify_archive() {
    # verify_archive <path> <expected-sha> -> 0 match, 1 mismatch, 2 missing
    local path="$1" want="$2"
    [[ -f "$path" ]] || return 2
    [[ "$(sha_of "$path")" == "$want" ]]
}

download_archive() {
    # download_archive <url> <dest> <referer>
    local url="$1" dest="$2" referer="$3"
    local tmp="${dest}.part"
    local -a curl_args=(-fL --retry 4 --retry-delay 3 --retry-all-errors
                        --connect-timeout 30 --max-time 3600
                        -A "$UA" --progress-bar -o "$tmp")
    [[ -n "$referer" ]] && curl_args+=(-e "$referer")
    curl "${curl_args[@]}" "$url"
    mv "$tmp" "$dest"
}

extract_archive() {
    # extract_archive <archive> <target-dir> [key]
    #
    # The optional key selects per-dataset exclusions. These are not cosmetic:
    # meetingbank ships a byte-identical duplicate tree (~180 MB) and the rev
    # tarball carries ~590 MB of audio and unrelated corpora. Both are pure waste
    # on disk and both mislead anyone who later greps the extract for "the data".
    local archive="$1" target="$2" key="${3:-}"
    local -a zip_excl=() tar_excl=()
    case "$key" in
        meetingbank) zip_excl=(-x 'uploaded/*' '__MACOSX/*' '.DS_Store') ;;
        earnings)    tar_excl=(--exclude='*/media' --exclude='*/media/*'
                               --exclude='*/longform_reconstitution*'
                               --exclude='*/coraal-multi*') ;;
    esac
    case "$archive" in
        *.zip)
            mkdir -p "$target"
            unzip -q -o "$archive" -d "$target" "${zip_excl[@]}"
            ;;
        *.tar.gz|*.tgz)
            # The QMSum tarball already contains its own top-level QMSum-<sha>/ dir,
            # so it extracts into the dataset dir, not into $target. Same for the
            # pinned rev speech-datasets tarball.
            tar -xzf "$archive" -C "$(dirname "$target")" "${tar_excl[@]}"
            ;;
        *)
            echo -e "${RED}Don't know how to extract ${archive}${NC}"
            return 1
            ;;
    esac
}

human() { du -sh "$1" 2>/dev/null | cut -f1; }

# --- Main loop ---------------------------------------------------------------
if $VERIFY_ONLY; then
    echo -e "${BLUE}Verifying corpora under ${DATA_DIR}${NC}\n"
else
    print_licenses
    echo -e "${GREEN}Licences accepted.${NC} Target: ${DATA_DIR}\n"
fi

for key in "${DATASETS[@]}"; do
    selected "$key" || continue

    sub="$DATA_DIR/$(field "$key" SUBDIR)"
    archive="$sub/$(field "$key" ARCHIVE)"
    want="$(field "$key" SHA256)"
    extract="$sub/$(field "$key" EXTRACT)"
    tier="$(field "$key" TIER)"

    echo -e "${BLUE}--- ${key} [Tier ${tier}]: $(field "$key" NAME) ---${NC}"

    if tier_blocked "$tier"; then
        echo -e "  ${YELLOW}– skipped: Tier ${tier} (non-commercial / restricted).${NC}"
        echo -e "    Re-run with ${YELLOW}--accept-noncommercial${NC} to fetch it. It may be used"
        echo -e "    locally but NO metric derived from it may ever be published."
        echo ""
        continue
    fi

    if $VERIFY_ONLY; then
        if verify_archive "$archive" "$want"; then
            echo -e "  archive  ${GREEN}✓ sha256 ok${NC} ($(human "$archive"))"
        else
            case $? in
                2) echo -e "  archive  ${RED}✗ missing${NC} — $archive"
                   FAILURES+=("$key: archive missing") ;;
                *) echo -e "  archive  ${RED}✗ SHA256 MISMATCH${NC}"
                   echo -e "           expected $want"
                   echo -e "           actual   $(sha_of "$archive")"
                   FAILURES+=("$key: checksum mismatch") ;;
            esac
        fi
        if [[ -d "$extract" ]]; then
            echo -e "  extract  ${GREEN}✓ present${NC} ($(human "$extract"))"
        else
            echo -e "  extract  ${YELLOW}– not extracted${NC}"
        fi
        echo ""
        continue
    fi

    # 1. Archive
    if ! $FORCE && verify_archive "$archive" "$want"; then
        echo -e "  ${GREEN}✓ archive already present with matching checksum — skipping download${NC}"
    else
        mkdir -p "$sub"
        echo -e "  ${YELLOW}↓ downloading${NC} $(field "$key" URL)"
        if ! download_archive "$(field "$key" URL)" "$archive" "$(field "$key" REFERER)"; then
            echo -e "  ${RED}✗ download failed${NC}"
            FAILURES+=("$key: download failed")
            echo ""
            continue
        fi
        got="$(sha_of "$archive")"
        if [[ "$got" != "$want" ]]; then
            echo -e "  ${RED}✗ SHA256 MISMATCH after download${NC}"
            echo -e "    expected $want"
            echo -e "    actual   $got"
            echo -e "    Upstream may have re-cut the file. Verify at $(field "$key" HOMEPAGE)"
            echo -e "    before changing the pinned hash in this script."
            FAILURES+=("$key: checksum mismatch after download")
            echo ""
            continue
        fi
        echo -e "  ${GREEN}✓ downloaded and verified${NC} ($(human "$archive"))"
    fi

    # 2. Extract
    if [[ -d "$extract" ]] && ! $FORCE; then
        echo -e "  ${GREEN}✓ already extracted${NC} → $extract ($(human "$extract"))"
    else
        echo -e "  ${YELLOW}⇱ extracting${NC} → $extract"
        if extract_archive "$archive" "$extract" "$key"; then
            echo -e "  ${GREEN}✓ extracted${NC} ($(human "$extract"))"
        else
            FAILURES+=("$key: extract failed")
        fi
    fi
    echo ""
done

# --- Multilingual arms (issue #403) ------------------------------------------
#
# Different shape from the two archive corpora above: multi-file, so the unit of
# pinning is a manifest file rather than a single SHA256 constant.

ml_manifest_path() { echo "$MANIFEST_DIR/$1.tsv"; }

# Rebuild a pinned manifest from upstream metadata. Opt-in (--refresh-manifest)
# because a moved checksum is a finding to investigate, not a file to regenerate.
ml_build_manifest() {
    local key="$1" src repo rev inc exc base out url hdr body f
    src="$(mlfield "$key" SOURCE)"
    out="$(ml_manifest_path "$key")"
    mkdir -p "$MANIFEST_DIR"
    : > "$out.new"
    case "$src" in
        hf:*)
            repo="${src#hf:}"; rev="${repo##*@}"; repo="${repo%@*}"
            inc="$(mlfield "$key" INCLUDE)"; exc="$(mlfield "$key" EXCLUDE)"
            url="https://huggingface.co/api/datasets/${repo}/tree/${rev}?recursive=true&limit=1000"
            hdr="$(mktemp)"; body="$(mktemp)"
            # The listing is paginated; without following Link: rel="next" a big repo
            # silently yields a short manifest, which then "verifies" against a
            # partial download.
            while [[ -n "$url" ]]; do
                if ! curl -sfL -A "$UA" -D "$hdr" -o "$body" "$url"; then
                    rm -f "$hdr" "$body" "$out.new"; return 1
                fi
                jq -r --arg inc "$inc" --arg exc "$exc" --arg repo "$repo" --arg rev "$rev" '
                    .[] | select(.type=="file")
                        | select(.path|test($inc)) | select(.path|test($exc)|not)
                        | "\(.lfs.oid // "-")\t\(.path)\thttps://huggingface.co/datasets/\($repo)/resolve/\($rev)/\(.path)"' \
                    "$body" >> "$out.new"
                url="$(grep -i '^link:' "$hdr" | sed -n 's/.*<\([^>]*\)>; rel="next".*/\1/p' | tr -d '\r')"
            done
            rm -f "$hdr" "$body"
            ;;
        url:*)
            base="${src#url:}"
            for f in $(mlfield "$key" FILES); do
                printf -- "-\t%s\t%s%s\n" "$f" "$base" "$f" >> "$out.new"
            done
            ;;
        *)
            echo -e "${RED}${key}: unrecognised SOURCE '${src}'${NC}"; rm -f "$out.new"; return 1 ;;
    esac
    sort -k2,2 "$out.new" > "$out"
    rm -f "$out.new"
    echo -e "  ${GREEN}✓ manifest rebuilt${NC} ($(wc -l < "$out") files) → $out"
}

# Replace every unresolved "-" hash with the SHA256 of the file actually on disk.
# Hub-hosted LFS objects advertise their SHA256; small non-LFS files and plain HTTP
# sources do not, so they are pinned the first time they are fetched.
ml_seal_manifest() {
    local key="$1" sub out tmp want rel url
    sub="$DATA_DIR/$(mlfield "$key" SUBDIR)"
    out="$(ml_manifest_path "$key")"
    tmp="$(mktemp)"
    while IFS=$'\t' read -r want rel url; do
        if [[ "$want" == "-" && -f "$sub/$rel" ]]; then
            want="$(sha_of "$sub/$rel")"
        fi
        printf '%s\t%s\t%s\n' "$want" "$rel" "$url" >> "$tmp"
    done < "$out"
    mv "$tmp" "$out"
}

ml_fetch_one() {
    # ml_fetch_one <sha|-> <relpath> <url> <dest-root> <force>
    local want="$1" rel="$2" url="$3" root="$4" force="$5" out got
    out="$root/$rel"
    mkdir -p "$(dirname "$out")"
    if [[ "$force" != "true" && -f "$out" ]]; then
        if [[ "$want" == "-" ]]; then
            [[ -s "$out" ]] && return 0
        elif [[ "$(sha256sum "$out" | cut -d' ' -f1)" == "$want" ]]; then
            return 0
        fi
    fi
    curl -sfL --retry 5 --retry-delay 3 --retry-all-errors --connect-timeout 30 \
         --max-time 7200 -A "$MLUA" -o "$out.part" "$url" || {
        echo "DOWNLOAD-FAILED $rel"; rm -f "$out.part"; return 1; }
    got="$(sha256sum "$out.part" | cut -d' ' -f1)"
    if [[ "$want" != "-" && "$got" != "$want" ]]; then
        echo "SHA-MISMATCH $rel expected=$want actual=$got"
        rm -f "$out.part"; return 1
    fi
    mv "$out.part" "$out"
}
MLUA="$UA"
export MLUA
export -f ml_fetch_one

if ml_any_selected || rb_any_selected || beir_any_selected; then
    echo -e "${BLUE}================================================================${NC}"
    echo -e "${BLUE}  Multi-file corpora: multilingual + retrieval + BEIR (#403)${NC}"
    echo -e "${BLUE}================================================================${NC}\n"
fi

# One loop, three groups: identical fetch mechanics, separate generated manifests.
for key in "${ML_DATASETS[@]}" "${RB_DATASETS[@]}" "${BEIR_DATASETS[@]}"; do
    selected "$key" || continue

    tier="$(mlfield "$key" TIER)"
    sub="$DATA_DIR/$(mlfield "$key" SUBDIR)"
    man="$(ml_manifest_path "$key")"

    echo -e "${BLUE}--- ${key} [Tier ${tier}]: $(mlfield "$key" NAME) ---${NC}"

    if tier_blocked "$tier"; then
        echo -e "  ${YELLOW}– skipped: Tier ${tier} (non-commercial / restricted).${NC}"
        echo -e "    Re-run with ${YELLOW}--accept-noncommercial${NC} to fetch it. It may be used"
        echo -e "    locally but NO metric derived from it may ever be published."
        echo ""
        continue
    fi

    if $REFRESH_MANIFEST; then
        command -v jq >/dev/null 2>&1 || {
            echo -e "  ${RED}✗ --refresh-manifest needs jq${NC}"; FAILURES+=("$key: jq missing"); echo ""; continue; }
        echo -e "  ${YELLOW}↻ rebuilding pinned manifest from $(mlfield "$key" SOURCE)${NC}"
        ml_build_manifest "$key" || { FAILURES+=("$key: manifest rebuild failed"); echo ""; continue; }
    fi

    if [[ ! -f "$man" ]]; then
        echo -e "  ${RED}✗ no pinned manifest at ${man}${NC}"
        echo -e "    Create it with: $0 --refresh-manifest --accept-licenses --only $key"
        FAILURES+=("$key: manifest missing")
        echo ""
        continue
    fi

    total="$(wc -l < "$man")"

    if $VERIFY_ONLY; then
        missing=0; bad=0; ok=0
        while IFS=$'\t' read -r want rel url; do
            if [[ ! -f "$sub/$rel" ]]; then missing=$((missing+1))
            elif [[ "$want" == "-" ]]; then ok=$((ok+1))
            elif [[ "$(sha_of "$sub/$rel")" == "$want" ]]; then ok=$((ok+1))
            else bad=$((bad+1)); echo -e "    ${RED}✗ ${rel}${NC}"; fi
        done < "$man"
        if [[ $missing -eq 0 && $bad -eq 0 ]]; then
            echo -e "  ${GREEN}✓ ${ok}/${total} files, sha256 ok${NC} ($(human "$sub"))"
        else
            echo -e "  ${RED}✗ ${ok} ok · ${missing} missing · ${bad} mismatched${NC} (of ${total})"
            [[ $bad -gt 0 ]] && FAILURES+=("$key: ${bad} checksum mismatch")
            [[ $missing -gt 0 ]] && FAILURES+=("$key: ${missing} files missing")
        fi
        echo ""
        continue
    fi

    echo -e "  ${YELLOW}↓ ${total} files → ${sub}${NC}"
    mkdir -p "$sub"
    # xargs rather than a serial loop: these corpora are hundreds of files and the
    # transfer, not the disk, is the bottleneck.
    if awk -F'\t' -v r="$sub" -v f="$FORCE" \
           '{printf "%s\t%s\t%s\t%s\t%s\n", $1, $2, $3, r, f}' "$man" \
       | xargs -d '\n' -P "$PARALLEL" -I{} bash -c \
           'IFS=$'"'"'\t'"'"' read -r a b c d e <<< "$1"; ml_fetch_one "$a" "$b" "$c" "$d" "$e"' _ {}
    then
        if $REFRESH_MANIFEST; then ml_seal_manifest "$key"; fi
        echo -e "  ${GREEN}✓ ${total} files present and verified${NC} ($(human "$sub"))"
    else
        echo -e "  ${RED}✗ one or more files failed — see DOWNLOAD-FAILED / SHA-MISMATCH above${NC}"
        echo -e "    A mismatch means upstream re-cut the file. Confirm at $(mlfield "$key" HOMEPAGE)"
        echo -e "    before re-pinning with --refresh-manifest."
        FAILURES+=("$key: download/checksum failure")
    fi
    echo ""
done

# --- Multilingual manifest ----------------------------------------------------
# Deliberately a SEPARATE file from the top-level MANIFEST.tsv: three agents fetch
# into this tree and a shared generated file is a merge conflict every time.
if ! $VERIFY_ONLY && ml_any_selected && [[ ${#FAILURES[@]} -eq 0 ]]; then
    mldir="$DATA_DIR/multilingual"
    if [[ -d "$mldir" ]]; then
        mlmanifest="$mldir/MANIFEST.tsv"
        # Same spirit as the top-level MANIFEST.tsv, one column wider. These corpora
        # are hundreds of files rather than one archive, so the per-dataset checksum
        # is the SHA256 OF THE PINNED MANIFEST — a single value that changes if any
        # member file's hash changes, and that folds into the top-level table as the
        # "sha256" column when the two are merged.
        {
            printf 'dataset\ttier\tfiles\tbytes\tmanifest_sha256\tlicense\tsource_url\tfetched_utc\n'
            for key in "${ML_DATASETS[@]}"; do
                sub="$DATA_DIR/$(mlfield "$key" SUBDIR)"
                [[ -d "$sub" ]] || continue
                man="$(ml_manifest_path "$key")"
                printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
                    "$key" "$(mlfield "$key" TIER)" \
                    "$(find "$sub" -type f | wc -l)" \
                    "$(du -sb "$sub" | cut -f1)" \
                    "$([[ -f "$man" ]] && sha_of "$man" || echo "-")" \
                    "$(mlfield "$key" LICENSE)" \
                    "$(mlfield "$key" HOMEPAGE)" \
                    "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
            done
        } > "$mlmanifest"
        echo -e "${BLUE}Multilingual manifest written:${NC} $mlmanifest"
    fi
fi

# --- Retrieval-benchmark manifest ---------------------------------------------
# Its own file for the same reason the multilingual one is: separate agents, no
# shared generated file, no merge conflict.
if ! $VERIFY_ONLY && rb_any_selected && [[ ${#FAILURES[@]} -eq 0 ]]; then
    rbdir="$DATA_DIR/retrieval-benchmarks"
    if [[ -d "$rbdir" ]]; then
        rbmanifest="$rbdir/MANIFEST.tsv"
        {
            printf 'dataset\ttier\tfiles\tbytes\tmanifest_sha256\tlicense\tsource_url\tfetched_utc\n'
            for key in "${RB_DATASETS[@]}"; do
                sub="$DATA_DIR/$(mlfield "$key" SUBDIR)"
                [[ -d "$sub" ]] || continue
                man="$(ml_manifest_path "$key")"
                printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
                    "$key" "$(mlfield "$key" TIER)" \
                    "$(find "$sub" -type f | wc -l)" \
                    "$(du -sb "$sub" | cut -f1)" \
                    "$([[ -f "$man" ]] && sha_of "$man" || echo "-")" \
                    "$(mlfield "$key" LICENSE)" \
                    "$(mlfield "$key" HOMEPAGE)" \
                    "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
            done
        } > "$rbmanifest"
        echo -e "${BLUE}Retrieval-benchmark manifest written:${NC} $rbmanifest"
    fi
fi

# --- BEIR: extract pass (issue #403) ------------------------------------------
# A BEIR zip already contains its own <task>/ top level (verified: scifact.zip unpacks to scifact/corpus.jsonl, scifact/queries.jsonl,
# scifact/qrels/{train,test}.tsv), so unzipping into SUBDIR would give
# beir/scifact/scifact/corpus.jsonl. Unzipping into SUBDIR's PARENT makes the zip's
# own directory land exactly on SUBDIR, next to the archive it came from.
# The marker is corpus.jsonl rather than a directory, for the same reason: the
# directory always exists (it holds the zip), so it can never signal "extracted".
beir_extract() {
    local key="$1" sub spec archive marker
    sub="$DATA_DIR/$(mlfield "$key" SUBDIR)"
    for spec in $(mlfield "$key" EXTRACTS); do
        archive="$sub/${spec%%:*}"
        marker="$sub/${spec#*:}"
        [[ -f "$archive" ]] || continue
        if [[ -e "$marker" ]] && ! $FORCE; then
            echo -e "  ${GREEN}✓ already extracted${NC} → ${marker#"$DATA_DIR"/}"
            continue
        fi
        echo -e "  ${YELLOW}⇱ extracting${NC} ${spec%%:*}"
        unzip -q -o "$archive" -d "$(dirname "$sub")" \
            || { FAILURES+=("$key: extract failed"); continue; }
        [[ -e "$marker" ]] || {
            echo -e "  ${RED}✗ ${spec%%:*} did not produce ${marker#"$DATA_DIR"/}${NC}"
            echo -e "    Upstream may have changed the zip's internal layout."
            FAILURES+=("$key: extract produced no ${spec#*:}"); }
    done
}

if ! $VERIFY_ONLY && beir_any_selected && [[ ${#FAILURES[@]} -eq 0 ]]; then
    for key in "${BEIR_DATASETS[@]}"; do
        selected "$key" || continue
        [[ -n "$(mlfield "$key" EXTRACTS)" ]] || continue
        tier_blocked "$(mlfield "$key" TIER)" && continue
        echo -e "${BLUE}--- ${key}: extract ---${NC}"
        beir_extract "$key"
        echo ""
    done
fi

# --- BEIR manifest (issue #403) -----------------------------------------------
# Fourth generated table, fourth separate file, same reason as the other three.
if ! $VERIFY_ONLY && beir_any_selected && [[ ${#FAILURES[@]} -eq 0 ]]; then
    beirdir="$DATA_DIR/beir"
    if [[ -d "$beirdir" ]]; then
        beirmanifest="$beirdir/MANIFEST.tsv"
        {
            printf 'dataset\ttier\tfiles\tbytes\tmanifest_sha256\tlicense\tsource_url\tfetched_utc\n'
            for key in "${BEIR_DATASETS[@]}"; do
                sub="$DATA_DIR/$(mlfield "$key" SUBDIR)"
                [[ -d "$sub" ]] || continue
                man="$(ml_manifest_path "$key")"
                printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
                    "$key" "$(mlfield "$key" TIER)" \
                    "$(find "$sub" -type f | wc -l)" \
                    "$(du -sb "$sub" | cut -f1)" \
                    "$([[ -f "$man" ]] && sha_of "$man" || echo "-")" \
                    "$(mlfield "$key" LICENSE)" \
                    "$(mlfield "$key" HOMEPAGE)" \
                    "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
            done
        } > "$beirmanifest"
        echo -e "${BLUE}BEIR manifest written:${NC} $beirmanifest"
    fi
fi

# --- Manifest ----------------------------------------------------------------
# Written on every successful fetch so the on-disk state is self-describing even
# if README.md drifts. Not a substitute for the pinned hashes above.
# Skipped when --only named a multilingual, retrieval-benchmark, document or BEIR
# key: that run touched nothing this manifest describes, and it is shared with the
# other arms.
if ! $VERIFY_ONLY && ! ml_only && ! rb_only && ! beir_only && [[ ${#FAILURES[@]} -eq 0 ]]; then
    manifest="$DATA_DIR/MANIFEST.tsv"
    {
        printf 'dataset\ttier\tarchive\tsha256\tbytes\tlicense\tsource_url\tfetched_utc\n'
        for key in "${DATASETS[@]}"; do
            sub="$DATA_DIR/$(field "$key" SUBDIR)"
            archive="$sub/$(field "$key" ARCHIVE)"
            [[ -f "$archive" ]] || continue
            printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
                "$key" "$(field "$key" TIER)" \
                "$(field "$key" ARCHIVE)" "$(sha_of "$archive")" \
                "$(stat -c%s "$archive")" "$(field "$key" LICENSE)" \
                "$(field "$key" URL)" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        done
    } > "$manifest"
    echo -e "${BLUE}Manifest written:${NC} $manifest"
fi

# --- Summary -----------------------------------------------------------------
echo -e "${BLUE}================================================================${NC}"
if [[ ${#FAILURES[@]} -eq 0 ]]; then
    echo -e "${GREEN}All selected datasets present and verified.${NC}"
    echo -e "Root: ${DATA_DIR} ($(human "$DATA_DIR") total)"
    exit 0
fi
echo -e "${RED}Problems:${NC}"
for f in "${FAILURES[@]}"; do echo -e "  ${RED}✗ $f${NC}"; done
exit 1
