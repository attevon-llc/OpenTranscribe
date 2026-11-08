---
sidebar_position: 2
title: Third-party licenses
description: Every runtime dependency whose license is not permissive, what it is for, where it is used, and why it is compatible with OpenTranscribe's AGPL-3.0
---

# Third-party licenses

OpenTranscribe's own code is licensed under the **GNU Affero General Public License v3.0**
(AGPL-3.0; see `LICENSE`). Almost every dependency is permissive (MIT, BSD, Apache-2.0, ISC).
This page covers the ones that are not: copyleft, vendor, or otherwise unusual terms. The
copyright holder of the project's own code is "OpenTranscribe". Third-party attribution is in
`NOTICE`, and the full machine-generated inventory is in
`security-reports/license-compliance/` (`pinned-requirements-licenses.csv`).

:::note This page is checked, not trusted
`backend/tests/unit/test_third_party_license_doc.py` reads the generated inventory and fails
when a package classified REVIEW, BLOCKER, PROPRIETARY-REVIEW or UNKNOWN is not named on this
page. Regenerate the inventory with `scripts/check-licenses.py` after changing a pin, then add
the new package here.
:::

## Why these licenses are compatible with AGPL-3.0

- **GPL-2.0-or-later** code may be combined with AGPL-3.0 work: the "or later" lets it be used
  under GPL-3.0, and GPL-3.0 section 13 explicitly allows linking with AGPL-3.0 (FSF license
  list, "GNU Affero General Public License" and "GPLv3 and AGPLv3" entries,
  https://www.gnu.org/licenses/license-list.html).
- **LGPL-2.1/LGPL-3.0** libraries may be used by a work under any license when the library stays
  separately replaceable. We install them unmodified from PyPI as ordinary packages. We never
  vendor or patch them, so a user can swap in another build (LGPL-3.0 section 4).
- **MPL-2.0** is file-level copyleft. It is GPL-compatible through its Secondary License
  mechanism (MPL-2.0 section 3.3; MPL FAQ, "Is MPL-2.0 compatible with the GPL?",
  https://www.mozilla.org/en-US/MPL/2.0/FAQ/). We do not modify any MPL file, so the only
  obligation, keeping the source of MPL files available, is met by the upstream project.
- Because the project is itself AGPL-3.0, the corresponding source of everything we change is
  already public in this repository.

## Python packages (backend)

| Package | What | Why we use it | Where | License | Compatibility and compliance |
|---|---|---|---|---|---|
| `mutagen` | Audio tag reader/writer | Optional `yt-dlp[default]` dependency that lets yt-dlp read and embed media metadata when importing URLs | Backend URL ingestion only (yt-dlp, `backend/requirements.txt`); never imported by our code | GPL-2.0-or-later | Used under GPL-3.0, which section 13 allows with AGPL-3.0. Unmodified, installed from PyPI, source on PyPI. Recorded as an owner-reviewed exception in `scripts/licenses-allowlist.txt` |
| `fpdf2` | PDF generation | Renders PDF transcript documents with numbered lines | `backend/app/services/transcript_document/pdf_renderer.py` | LGPL-3.0-only | Unmodified, separately installed package, dynamically imported; replaceable by the user |
| `ldap3` | Pure-Python LDAP client | LDAP and Active Directory sign-in | `backend/app/auth/ldap_*.py`, `direct_auth.py` | LGPL-3.0 | Unmodified and replaceable as above |
| `psycopg2` | PostgreSQL driver (built from source, not the `-binary` wheel) | Database access for migrations and role setup | `backend/app/db/` | LGPL-2.1-or-later | Unmodified shared library, replaceable; source build keeps it linked against the system libpq |
| `certifi` | Mozilla CA bundle | TLS trust store for outbound HTTPS and the object store client | `backend/app/services/minio_service.py`, requests/httpx | MPL-2.0 | Unmodified; the bundle's source is Mozilla's published root list |
| `tqdm` | Progress bars | Transitive dependency of the ASR and model-download stack (whisperx, huggingface-hub) | Model download and ASR workers | MPL-2.0 AND MIT | Unmodified |
| `nvidia-*` (`nvidia-cublas-cu12`, `nvidia-cudnn-cu12`, ...) | NVIDIA CUDA runtime libraries | GPU inference via PyTorch | GPU backend image only | NVIDIA proprietary EULA | Not copyleft and not linked into our source; redistributed unmodified as NVIDIA permits. Not in the CPU-only (lite) image |
| `cuda-toolkit` | NVIDIA meta-package | Pulls in the CUDA libraries above | GPU backend image only | none declared (ships no code) | Nothing to comply with itself |
| `azure-cognitiveservices-speech` | Microsoft Speech SDK | Optional cloud ASR provider | `backend/app/services/asr/azure_provider.py` | Microsoft proprietary | Used only if an admin configures Azure; unmodified; not AGPL-linked source |
| `pyannote*` (`pyannote-audio`, `-core`, `-database`, `-metrics`, `-pipeline`, `pyannoteai-sdk`) | Speaker diarization | Diarization (our GPU-optimized fork of pyannote-audio plus its companions) | Backend diarization | MIT (wheel metadata declares none; the shipped LICENSE files are MIT) | Permissive; flagged only because the metadata field is empty |
| `torchcodec` | Audio/video decoding for PyTorch | Media decoding in the ML stack | Backend, via torch | BSD-3-Clause (the metadata embeds the text rather than an identifier) | Permissive; flagged only because the metadata field is free text |

## Frontend packages

| Package | What | Why | Where | License | Compliance |
|---|---|---|---|---|---|
| `lightningcss*` (platform binaries) | CSS parser and minifier | Used by the Tailwind/Vite toolchain to build the CSS | Build time only; not present in the shipped nginx image | MPL-2.0 | Unmodified; no MPL file is distributed in the runtime image |

## Container images and OS packages

| Component | What | Why | Where | License | Compliance |
|---|---|---|---|---|---|
| `ca-certificates` (OS package) | Debian/Alpine CA bundle | TLS trust store | Backend and frontend images | MPL-2.0 with GPL-2.0-or-later parts (scripts) | Unmodified distro package; source from the distro |
| Silo (`pgsty/silo`) | S3-compatible object store, an AGPL-3.0 fork of MinIO | Bundled object storage for media and artifacts (replaced MinIO in 0.6.0) | `docker-compose.yml` service `minio`; see [Object storage](../operations/object-storage.md) | AGPL-3.0 | Same license as OpenTranscribe. We run the unmodified upstream image pulled from `docker.io/pgsty/silo`, pinned by digest, and do not redistribute a mirror. Its source is published by the upstream project under AGPL-3.0, and any network user can obtain it there |

## Sources consulted

- FSF license list and GPL compatibility: https://www.gnu.org/licenses/license-list.html
- GNU AGPL-3.0, section 13: https://www.gnu.org/licenses/agpl-3.0.html
- GNU LGPL-3.0, section 4: https://www.gnu.org/licenses/lgpl-3.0.html
- MPL-2.0 text and FAQ (combining with GPL): https://www.mozilla.org/en-US/MPL/2.0/ and
  https://www.mozilla.org/en-US/MPL/2.0/FAQ/
- Each package's own metadata, recorded in `pinned-requirements-licenses.csv`

This page is engineering documentation, not legal advice.
