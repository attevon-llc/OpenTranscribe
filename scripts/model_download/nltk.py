"""The NLTK data download group, including the hardlinked-cache repair."""

import os
from pathlib import Path

from model_download.ui import (
    print_error,
    print_header,
    print_info,
    print_success,
    print_warning,
)


def _rewrite_as_own_inode(path):
    """Replace ``path`` by a private copy of itself. Returns True if a link was broken.

    The copy goes to a ``tempfile.mkstemp`` file in the same directory (``O_EXCL``, a random
    name, so a pre-planted file or symlink at a guessable name is never written through),
    the source is opened ``O_NOFOLLOW`` and judged by ``fstat`` of what was opened (so a
    path swapped for a symlink, or already rewritten by a concurrent run, is not copied),
    the copy and then the directory are fsynced around the ``os.replace``, and the temp file
    is removed on any failure.
    """
    import shutil
    import stat
    import tempfile

    src_fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        info = os.fstat(src_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink <= 1:
            return False
        directory, name = os.path.split(path)
        tmp_fd, tmp = tempfile.mkstemp(prefix=f'.{name}.', suffix='.dehl', dir=directory)
        try:
            with os.fdopen(tmp_fd, 'wb') as dst, os.fdopen(os.dup(src_fd), 'rb') as src:
                shutil.copyfileobj(src, dst)
                dst.flush()
                os.fchmod(dst.fileno(), stat.S_IMODE(info.st_mode))
                os.fsync(dst.fileno())
            os.utime(tmp, ns=(info.st_atime_ns, info.st_mtime_ns))
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError as cleanup_error:
                print_warning(f'  Could not remove temp file {tmp}: {cleanup_error}')
            raise
    finally:
        os.close(src_fd)
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return True


def break_nltk_hardlinks(root):
    """Give every multiply-linked file under ``root`` its own inode, content unchanged.

    nltk >= 3.10 (``nltk/pathsec.py``, CWE-59) refuses to open any file whose ``st_nlink``
    exceeds one, and ``nltk.download`` reads the existing package zips before deciding
    whether to fetch. A cache seeded with ``cp -al``, ``rsync --link-dest`` or a dedup tool
    therefore failed every package in the group -- one linked ``punkt_tab.zip`` failed both
    ``punkt_tab`` and ``punkt``. The refusal is intended, so the data is made to comply:
    each linked file is copied and atomically renamed over the original
    (``_rewrite_as_own_inode``), leaving any other name for the old inode untouched.
    nltk_data is ~50 MB, so the duplicated bytes are negligible (unlike the huggingface and
    torch caches, which are left alone). Symlinks are skipped: nltk judges a link itself.

    Only the standard library is used, so this does not depend on the image's ``app``
    package. (The rest of this script does: the NLTK group imports
    ``app.utils.nltk_offline``, the PyAnnote group ``app.transcription.diarizer`` and
    ``app.services.embedding_mode_service``, from the image it runs in.)
    ``scripts/common.sh``'s ``ensure_nltk_data_unlinked`` does the same on the host for
    ``./opentr.sh start``, which runs this downloader only when the corpora are missing.

    Args:
        root: The NLTK data directory.

    Returns:
        ``(rewritten, unfixable)``: how many files were given their own inode, and the paths
        that could not be (read-only mount, directory owned by another account).
    """
    import stat

    rewritten = 0
    unfixable = []
    for current, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(current, name)
            try:
                info = os.lstat(path)
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink <= 1:
                continue
            try:
                if _rewrite_as_own_inode(path):
                    rewritten += 1
            except OSError:
                unfixable.append(path)
    return rewritten, unfixable


def download_nltk_data():
    """Download NLTK data files required by transformers/whisperx"""
    print_header('Downloading NLTK Data')

    try:
        import nltk

        # Honour $NLTK_DATA, which is what the images actually set (issue #491).
        #
        # This was hardcoded to `Path.home()/.cache/nltk_data`, and it matched the
        # image only by luck of $HOME: Dockerfile.prod and Dockerfile.lite run as
        # `appuser`, but Dockerfile.blackwell runs as `user` and sets
        # NLTK_DATA=/home/user/.cache/nltk_data. So on the Blackwell image this
        # fetched the corpora into a directory nothing reads, reported success,
        # and left the deployment to discover the gap at runtime.
        nltk_data_path = Path(os.environ.get('NLTK_DATA') or (Path.home() / '.cache' / 'nltk_data'))
        nltk_data_path.mkdir(parents=True, exist_ok=True)

        rewritten, unfixable = break_nltk_hardlinks(str(nltk_data_path))
        if rewritten:
            print_info(
                f'Gave {rewritten} multiply-linked NLTK file(s) their own inode '
                '(nltk >= 3.10 refuses hardlinked files)'
            )
        if unfixable:
            print_error(
                f'{len(unfixable)} NLTK file(s) are hardlinked and could not be rewritten, '
                f'e.g. {unfixable[0]}'
            )
            print_error(
                'nltk >= 3.10 refuses multiply-linked files (pathsec, CWE-59), so every '
                'package would fail. The cache was probably seeded with `cp -al`, '
                '`rsync --link-dest` or a dedup tool.'
            )
            print_error(
                'Fix on the host (content is kept, only the inode changes), then re-run:\n'
                '     find <MODEL_CACHE_DIR>/nltk_data -type f -links +1 '
                '-exec sh -c \'cp -p "$1" "$1.dehl" && mv -f "$1.dehl" "$1"\' _ {} \\;'
            )
            return {
                'nltk': {
                    'status': 'failed',
                    'error': f'{len(unfixable)} multiply-linked file(s) under {nltk_data_path}',
                }
            }

        # nltk >= 3.10.4 refuses to install into (or later read from) a directory that is
        # group/world-writable, and the host-created bind mount usually is (umask 002).
        try:
            from app.utils.nltk_offline import ensure_nltk_data_private
        except ImportError:
            print_warning(
                'This backend image predates app.utils.nltk_offline, so group/world write '
                'is not stripped from the NLTK cache; if nltk later refuses it, run '
                '`chmod -R go-w` on the nltk_data directory, or use the image matching '
                "this script's release (OT_IMAGE_TAG)."
            )
        else:
            ensure_nltk_data_private([str(nltk_data_path)])

        # Add to NLTK's data path
        if str(nltk_data_path) not in nltk.data.path:
            nltk.data.path.insert(0, str(nltk_data_path))

        print_info(f'NLTK data path: {nltk_data_path}')

        # Download required NLTK data packages
        required_packages = [
            'punkt_tab',  # Punkt tokenizer (new tabular format)
            'punkt',  # Punkt tokenizer (legacy format for compatibility)
            'averaged_perceptron_tagger_eng',  # POS tagger for NER
            'maxent_ne_chunker_tab',  # Named entity chunker for speaker name extraction
            'words',  # English word corpus (required by NE chunker)
            # Topic/keyword extraction: app/utils/text_preprocessing.py and
            # services/ingest_artifacts/textrank.py. Absent here, it was fetched at RUNTIME on
            # first topic extraction — which fails on an airgapped deployment (issue #491).
            'stopwords',
        ]

        downloaded = []
        for package in required_packages:
            try:
                print_info(f'Downloading NLTK package: {package}')
                nltk.download(package, download_dir=str(nltk_data_path), quiet=False)
                downloaded.append(package)
                print_success(f'  Downloaded: {package}')
            except Exception as pkg_error:
                print_error(f'  Failed to download {package}: {pkg_error}')

        if downloaded:
            print_success(f'NLTK data downloaded successfully ({len(downloaded)} packages)')
        else:
            raise Exception('No NLTK packages were downloaded')

        return {
            'nltk': {'packages': downloaded, 'path': str(nltk_data_path), 'status': 'downloaded'}
        }

    except Exception as e:
        print_error(f'Failed to download NLTK data: {e}')
        return {'nltk': {'status': 'failed', 'error': str(e)}}
