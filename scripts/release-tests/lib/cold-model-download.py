#!/usr/bin/env python3
"""Oracle for the cold-cache model-download rehearsal (Scenario D).

v0.6.0 shipped a `scripts/download-models.py` whose `pyannote` group exited 1 on EVERY host,
CPU and GPU alike, from an empty cache: its warm-up fed synthetic sine-wave audio to whisperx,
VAD found no speech, and transformers 5.x indexed an empty batch (`IndexError: list index out of
range`). No gate saw it, for two independent reasons: the rehearsal installs with
OPENTRANSCRIBE_UNATTENDED=1, which makes setup-opentranscribe.sh skip `download_ai_models`
outright, and every scenario then seeds its model cache from a warm shared copy. The downloader a
real `curl | bash` user runs had never executed under any release gate.

This file is the part of that rehearsal that has to be RIGHT rather than merely run, so it is
plain Python with unit tests (backend/tests/unit/test_release_cold_model_download.py). Stdlib
only: it runs on the release host, not in the image.

Subcommands:
  groups <download-models.py>   the downloader's own --only groups, read from its
                                DOWNLOAD_GROUPS dict by AST. Derived, never transcribed: a new
                                group is rehearsed the day it lands.
  verify --cache DIR --groups a,b [--whisper-repo R] [--opensearch-models m1,m2]
                                independent artifact check per group, one TSV row each
                                (group, pass|fail, detail). Exit 0 all pass, 1 any fail,
                                3 could not check (no cache dir, unknown group).
  outcome --root DIR --criterion download|weights|nltk-hardlink
                                turn the scenario's TSVs into `outcome<TAB>detail` for
                                65-rehearse.sh's `record`. A missing TSV is `not-measured`,
                                never a pass.

Why artifacts are checked independently of the exit code: the downloader's `main()` fails only
on status == 'failed'. `download_redaction_models` returns 'partial' when a model fails, and the
diar-native group returns 'skipped' when it cannot import its provisioner — both exit 0 with
weights missing. An exit code is the downloader's opinion of itself; this is a second opinion.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

#: Model repos each group must leave in the HuggingFace hub cache. Every id here also appears
#: in download-models.py AND in the backend code that loads it; the unit test enforces both, so
#: the oracle cannot drift from either side without failing.
HF_REPOS: dict[str, tuple[str, ...]] = {
    'pyannote': ('pyannote/speaker-diarization-community-1',),
    'reranker': ('cross-encoder/ms-marco-MiniLM-L-6-v2',),
    'speaker-attributes': ('prithivMLmods/Common-Voice-Gender-Detection',),
    'redaction': (
        'knowledgator/gliner-pii-base-v1.0',
        'unitary/toxic-bert',
        'unitary/multilingual-toxic-xlm-roberta',
    ),
}

#: NLTK resources the app opens. Directory OR the zip nltk.download leaves beside it.
NLTK_RESOURCES = (
    'tokenizers/punkt_tab',
    'corpora/stopwords',
    'corpora/words',
    'taggers/averaged_perceptron_tagger_eng',
    'chunkers/maxent_ne_chunker_tab',
)

SENTENCE_TRANSFORMERS_MODEL = 'all-MiniLM-L6-v2'
DEFAULT_OPENSEARCH_MODELS = ('all-MiniLM-L6-v2',)
WEIGHT_SUFFIXES = ('.safetensors', '.bin', '.onnx', '.pt', '.pth', '.ckpt', '.npy', '.zip')

#: Every group this oracle knows how to check. The unit test asserts this EQUALS the
#: downloader's DOWNLOAD_GROUPS, so a new group without an expectation fails the fast suite.
KNOWN_GROUPS = frozenset(
    {'whisperx', 'nltk', 'sentence-transformers', 'opensearch', 'diar-native', *HF_REPOS}
)


def downloader_groups(script: Path) -> list[str]:
    """Return the keys of DOWNLOAD_GROUPS in download-models.py, in declaration order."""
    tree = ast.parse(script.read_text(), filename=str(script))
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else []
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Name) and target.id == 'DOWNLOAD_GROUPS':
                value = node.value
                if not isinstance(value, ast.Dict):
                    break
                keys = [k.value for k in value.keys if isinstance(k, ast.Constant)]
                if len(keys) != len(value.keys):
                    raise ValueError('DOWNLOAD_GROUPS has a non-literal key')
                return [str(k) for k in keys]
    raise ValueError(f'no literal DOWNLOAD_GROUPS dict in {script}')


def _weights_under(path: Path) -> list[Path]:
    if not path.is_dir():
        return []
    return [
        f
        for f in path.rglob('*')
        if f.is_file() and f.suffix in WEIGHT_SUFFIXES and f.stat().st_size > 0
    ]


def _hf_repo_check(cache: Path, repo_id: str) -> str | None:
    """None when a hub-layout cache holds a non-empty weight file for repo_id, else the reason.

    Two roots: huggingface/hub, and sentence-transformers/ — the image sets the
    sentence-transformers cache home there, so CrossEncoder (the chat reranker) writes its
    hub-layout repo under it, and the backend reads it from the same place (measured on the
    v0.6.0 image: the reranker exists ONLY there).
    """
    name = 'models--' + repo_id.replace('/', '--')
    roots = [cache / 'huggingface' / 'hub', cache / 'sentence-transformers']
    repo_dirs = [root / name for root in roots if (root / name).is_dir()]
    if not repo_dirs:
        return f'{repo_id}: not in huggingface/hub or sentence-transformers/'
    # snapshots/ holds the real file NAMES as symlinks into blobs/; is_file()/stat() follow
    # them, so a dangling link (an interrupted download) is not counted. model.bin is
    # CTranslate2's weight file name, so the faster-whisper repos match too.
    if not any(_weights_under(d / 'snapshots') for d in repo_dirs):
        return f'{repo_id}: no non-empty weight file in its snapshot'
    return None


def _nltk_check(cache: Path) -> list[str]:
    root = cache / 'nltk_data'
    problems = []
    for res in NLTK_RESOURCES:
        directory = root / res
        if not (directory.is_dir() and any(directory.iterdir())) and not (
            directory.with_suffix('.zip').is_file()
        ):
            problems.append(f'nltk_data/{res} missing')
    # nltk >= 3.10 pathsec refuses a multiply-linked corpus file (CWE-59); a downloader that
    # left one would hand the app a corpus it cannot open.
    linked = [f for f in root.rglob('*') if f.is_file() and f.stat().st_nlink > 1]
    if linked:
        problems.append(f'{len(linked)} multiply-linked file(s) under nltk_data, e.g. {linked[0]}')
    return problems


def check_group(
    cache: Path, group: str, whisper_repo: str | None, opensearch_models: tuple[str, ...]
) -> list[str]:
    """Return the problems found for one group (empty list = pass)."""
    problems: list[str] = []
    if group == 'whisperx':
        if not whisper_repo:
            return ["whisper repo id not resolved from the image's catalog (could not check)"]
        reason = _hf_repo_check(cache, whisper_repo)
        problems += [reason] if reason else []
    elif group in HF_REPOS:
        problems += [r for r in (_hf_repo_check(cache, rid) for rid in HF_REPOS[group]) if r]
    elif group == 'nltk':
        problems += _nltk_check(cache)
    elif group == 'sentence-transformers':
        st_root = cache / 'sentence-transformers'
        hits = [
            p
            for p in (st_root.iterdir() if st_root.is_dir() else [])
            if SENTENCE_TRANSFORMERS_MODEL in p.name
        ]
        if not any(_weights_under(p) for p in hits):
            problems.append(f'sentence-transformers/*{SENTENCE_TRANSFORMERS_MODEL}*: no weights')
    elif group == 'opensearch':
        for model in opensearch_models:
            model_dir = cache / 'opensearch-ml' / model
            if not _weights_under(model_dir):
                problems.append(f'opensearch-ml/{model}: no model artifact')
            config = model_dir / 'config.json'
            if not (config.is_file() and config.stat().st_size > 0):
                problems.append(
                    f'opensearch-ml/{model}/config.json missing (offline register 400s)'
                )
    elif group == 'diar-native':
        onnx = [f for f in _weights_under(cache / 'diar-native') if f.suffix == '.onnx']
        if not onnx:
            problems.append('diar-native: no .onnx export')
    else:
        problems.append(f"no expectation defined for group '{group}' (could not check)")
    return problems


def cmd_verify(args: argparse.Namespace) -> int:
    cache = Path(args.cache)
    if not cache.is_dir():
        print(f'cache dir {cache} does not exist', file=sys.stderr)
        return 3
    groups = [g for g in args.groups.split(',') if g]
    if not groups:
        print('no groups to verify', file=sys.stderr)
        return 3
    models = tuple(m for m in args.opensearch_models.split(',') if m) or DEFAULT_OPENSEARCH_MODELS
    rc = 0
    for group in groups:
        if group not in KNOWN_GROUPS:
            print(f'{group}\tfail\tno expectation defined (could not check)')
            rc = max(rc, 3)
            continue
        problems = check_group(cache, group, args.whisper_repo or None, models)
        print(f'{group}\t{"fail" if problems else "pass"}\t{"; ".join(problems)}')
        if problems:
            rc = max(rc, 1)
    return rc


def _read_tsv(path: Path) -> list[list[str]]:
    rows = []
    for line in path.read_text().splitlines():
        if line.strip() and not line.startswith('#'):
            rows.append(line.split('\t'))
    return rows


def _raw_outcome(root: Path, criterion: str) -> tuple[str, str]:
    name = {'download': 'groups.tsv', 'weights': 'artifacts.tsv', 'nltk-hardlink': 'hardlink.tsv'}
    path = root / name[criterion]
    if not path.is_file():
        return 'not-measured', f'{path.name} not written (scenario did not reach this check)'
    rows = _read_tsv(path)
    if not rows:
        return 'not-measured', f'{path.name} is empty'
    if criterion == 'nltk-hardlink':
        verdict = rows[0]
        word = verdict[0] if verdict[0] in ('pass', 'fail', 'not-measured') else 'not-measured'
        return word, verdict[1] if len(verdict) > 1 else ''

    if criterion == 'download':
        failed = [f'{r[0]}(rc={r[1] if len(r) > 1 else "?"})' for r in rows if r[1:2] != ['0']]
        if failed:
            return 'fail', 'groups failed from an empty cache: ' + ', '.join(failed)
    else:
        failed = [f'{r[0]}: {r[2] if len(r) > 2 else "?"}' for r in rows if r[1:2] != ['pass']]
        if failed:
            return 'fail', '; '.join(failed)

    # A clean run of SOME groups is not a clean run. The expected set is the downloader's own
    # DOWNLOAD_GROUPS, written by the scenario before anything ran.
    expected_file = root / 'groups-expected.txt'
    if not expected_file.is_file():
        return (
            'not-measured',
            'groups-expected.txt not written; cannot tell a full run from a subset',
        )
    expected = [g for g in expected_file.read_text().split() if g]
    missing = sorted(set(expected) - {r[0] for r in rows})
    if not expected or missing:
        return 'not-measured', f'{len(rows)} of {len(expected)} group(s) checked; missing: ' + (
            ', '.join(missing) or 'the expected list is empty'
        )
    verb = 'exited 0' if criterion == 'download' else 'left their weights'
    return 'pass', f'all {len(rows)} group(s) {verb}: ' + ','.join(r[0] for r in rows)


def outcome(root: Path, criterion: str) -> tuple[str, str]:
    """Map the scenario's evidence files to (pass|fail|not-measured, detail).

    A failure is reported as found. A pass needs one more fact: that the image was shown to be
    the code under test. With a DOWNLOADER_IMAGE override (how a historical image is
    re-examined) the run is evidence about THAT image, never about this release, so its pass
    becomes not-measured.
    """
    verdict, detail = _raw_outcome(root, criterion)
    if verdict != 'pass':
        return verdict, detail
    provenance = root / 'provenance.txt'
    text = provenance.read_text().strip() if provenance.is_file() else ''
    if not text.startswith('verified '):
        return 'not-measured', f'{detail} — but image provenance is not verified ({text or "none"})'
    return verdict, detail


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n', 1)[0])
    sub = parser.add_subparsers(dest='cmd', required=True)
    p_groups = sub.add_parser('groups')
    p_groups.add_argument('script')
    p_verify = sub.add_parser('verify')
    p_verify.add_argument('--cache', required=True)
    p_verify.add_argument('--groups', required=True)
    p_verify.add_argument('--whisper-repo', default='')
    p_verify.add_argument('--opensearch-models', default='')
    p_out = sub.add_parser('outcome')
    p_out.add_argument('--root', required=True)
    p_out.add_argument(
        '--criterion', required=True, choices=('download', 'weights', 'nltk-hardlink')
    )
    args = parser.parse_args(argv)

    if args.cmd == 'groups':
        try:
            print('\n'.join(downloader_groups(Path(args.script))))
        except (OSError, SyntaxError, ValueError) as exc:
            print(f"cannot read the downloader's groups: {exc}", file=sys.stderr)
            return 3
        return 0
    if args.cmd == 'verify':
        return cmd_verify(args)
    verdict, detail = outcome(Path(args.root), args.criterion)
    print(f'{verdict}\t{detail}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
