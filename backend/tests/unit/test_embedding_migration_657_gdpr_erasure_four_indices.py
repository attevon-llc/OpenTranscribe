"""GDPR Art. 17 voiceprint erasure must still cover all four indices (issue #657).

The task explicitly calls out ``gdpr_erasure_service.py``'s
``_erase_voiceprints`` (delete-by-query over ``{alias, v3, v4, v3_backup}``)
as a location that must not regress: its own docstring records that
``_restore_v3_from_backup`` once RESURRECTED erased biometric data. None of
the six #657 defect fixes touch this function, but it is exactly the kind of
site an unrelated refactor could silently narrow (e.g. by "simplifying" the
index set while fixing the alias-resolver disagreement in defect 2) — so this
pins the four-index set as a regression guard rather than relying on nobody
touching it by accident.
"""

from __future__ import annotations

import inspect

import pytest

from app.services import gdpr_erasure_service


@pytest.mark.unit
class TestVoiceprintErasureCoversAllFourIndices:
    def test_erasure_queries_alias_v3_v4_and_v3_backup(self):
        source = inspect.getsource(gdpr_erasure_service)
        # Locate the specific block building the erasure index set, not just
        # any mention of these four helpers anywhere in the file.
        marker = "indices = {"
        assert marker in source
        block = source[source.index(marker) :]
        block = block[: block.index("}") + 1]

        assert "get_speaker_index()" in block
        assert "get_speaker_index_v3()" in block
        assert "get_speaker_index_v4()" in block
        assert "get_speaker_index_v3_backup()" in block

    def test_docstring_still_warns_about_backup_resurrection(self):
        """The docstring recording the _restore_v3_from_backup incident is the
        institutional memory for why v3_backup is in the erasure set at all -
        pin its presence so a rewrite cannot silently drop the warning along
        with the behavior it explains.
        """
        source = inspect.getsource(gdpr_erasure_service)
        assert "speakers_v3_backup" in source
        assert "resurrect" in source.lower() or "restore_v3_from_backup" in source
