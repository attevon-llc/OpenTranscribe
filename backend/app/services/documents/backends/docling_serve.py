"""Sidecar parsing tier — ``docling-serve`` over HTTP, async submit + poll.

This is the tier that has the layout model, TableFormer and RapidOCR resident, so it is
the only one that can do OCR, table structure or reading-order recovery. It lives in its
own container precisely so those ~2 GB of models are **not** in the shared backend image
that ten worker containers and the API all run.

Three rules the plan sets and this module enforces:

* **Always the async convert + poll API, never the sync endpoint.** ``docling-serve``'s
  synchronous endpoint has a 2-minute server-side timeout; a 200-page scan exceeds it and
  the failure looks like a network error rather than a timeout.
* **Pin the image by digest.** ``docling-serve`` CUDA images are *never* tagged
  ``latest``, so a tag-based pin silently means "CPU forever" on a GPU host. The digest
  lives in ``docker-compose.yml``; :meth:`DoclingServeParser.health` records the version
  the sidecar reports so ``document.parser_version`` is the **remote** version, not this
  client's.
* **Unreachable is retryable, never fatal.** ``docling-serve#233`` has GPU memory climbing
  across batches, so the sidecar is on a periodic recycle; a parse that lands during a
  recycle must leave the document at ``ocr_pending`` for the retry sweep instead of
  burning it to ``failed``.
"""

from __future__ import annotations

import base64
import logging
import time
from typing import Any

from ..ir import IRBuilder
from ..ir import ParsedDocument
from ..safety import prescan
from ..types import DocumentEmptyError
from ..types import DocumentParseError
from ..types import DocumentParserUnavailableError
from ..types import ParseOptions
from ..types import ParseSource

logger = logging.getLogger(__name__)

#: Terminal task states in docling-serve's poll response.
_SUCCESS_STATES = frozenset({"success", "SUCCESS"})
_FAILURE_STATES = frozenset({"failure", "FAILURE", "revoked", "REVOKED"})

#: Poll cadence. Fast enough that a one-page conversion is not dominated by the wait,
#: slow enough that a 20-page OCR shard is not thousands of requests.
_POLL_INTERVAL_SECONDS = 1.0


class DoclingServeParser:
    """HTTP client for a ``docling-serve`` sidecar."""

    name = "docling.serve"

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
        poll_timeout: float = 1800.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.poll_timeout = poll_timeout
        self._version = "unknown"

    @property
    def version(self) -> str:
        """The **remote** version, learned at health-check time.

        A document converted by sidecar 1.30 and one converted by 1.34 are not
        interchangeable for a reparse sweep, and recording this client's version instead
        would make them indistinguishable.
        """
        return self._version

    def supports(self, mime: str, filename: str, *, needs_ocr: bool) -> bool:
        """Everything the slim tier does, plus OCR and layout-dependent formats."""
        return mime.startswith("image/") or mime in {
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "application/vnd.oasis.opendocument.text",
            "application/vnd.oasis.opendocument.spreadsheet",
            "application/epub+zip",
            "text/markdown",
            "text/html",
            "text/csv",
            "text/plain",
        }

    def health(self) -> tuple[bool, str]:
        """Probe ``/health``, then ``/version``. Never raises.

        Two calls because ``/health`` answers ``{"status": "ok"}`` and nothing else —
        verified against docling-serve 1.30.0. ``/version`` returns the whole stack
        (``docling-serve``, ``docling``, ``docling-parse``, ``docling-ibm-models``), and
        it is the *docling* version that decides whether two parses are comparable, so
        that is what lands on ``document.parser_version``.
        """
        try:
            import requests

            health = requests.get(f"{self.base_url}/health", timeout=self.timeout)
            health.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - health must degrade, not explode
            return False, f"sidecar unreachable at {self.base_url}: {exc}"

        try:
            import requests

            payload = requests.get(f"{self.base_url}/version", timeout=self.timeout).json()
        except Exception:  # noqa: BLE001 - reachable but version-less is still usable
            payload = {}
        serve = str(payload.get("docling-serve") or "unknown")
        core = str(payload.get("docling") or "unknown")
        self._version = f"docling-serve/{serve}+docling/{core}"
        return True, self._version

    # -- conversion ----------------------------------------------------------------

    def parse(self, source: ParseSource, *, options: ParseOptions) -> ParsedDocument:
        """Submit, poll, and turn the returned ``DoclingDocument`` JSON into our IR."""
        import requests

        data = source.read_bytes()
        prescan(source.mime, data, max_pages=options.max_pages)

        payload = self._build_request(source, data, options)
        try:
            submit = requests.post(
                f"{self.base_url}/v1/convert/source/async",
                json=payload,
                timeout=self.timeout,
            )
            submit.raise_for_status()
            task_id = submit.json()["task_id"]
        except requests.RequestException as exc:
            raise DocumentParserUnavailableError(
                "the document parser sidecar could not accept this document",
                detail=str(exc),
            ) from exc
        except (KeyError, ValueError) as exc:
            raise DocumentParseError(
                "the sidecar returned a response with no task id", detail=str(exc)
            ) from exc

        result = self._poll(task_id)
        return self._to_ir(result, source, options)

    def _build_request(
        self, source: ParseSource, data: bytes, options: ParseOptions
    ) -> dict[str, Any]:
        """Build the async-convert body. Content is inlined; the sidecar never fetches.

        Deliberately *not* the ``http`` source kind: letting the sidecar fetch a URL we
        were handed would be an SSRF primitive one container removed from the endpoint's
        ``assert_safe_outbound_url`` check.
        """
        do_ocr = options.ocr in ("auto", "force")
        convert_options: dict[str, Any] = {
            "do_ocr": do_ocr,
            "force_ocr": options.ocr == "force",
            "do_table_structure": True,
            "to_formats": ["json"],
            # Defaults to True, and would base64 every extracted figure into the response
            # — tens of MB of payload the IR has no field for and never reads.
            "include_images": False,
            "include_page_images": False,
        }
        if options.page_range is not None:
            convert_options["page_range"] = list(options.page_range)
        if options.language:
            convert_options["ocr_lang"] = [options.language]
        return {
            "options": convert_options,
            "sources": [
                {
                    "kind": "file",
                    "base64_string": base64.b64encode(data).decode("ascii"),
                    "filename": source.filename,
                }
            ],
        }

    def _poll(self, task_id: str) -> dict[str, Any]:
        """Poll until terminal, then fetch the result. Raises on timeout or failure."""
        import requests

        deadline = time.monotonic() + self.poll_timeout
        while True:
            if time.monotonic() > deadline:
                raise DocumentParserUnavailableError(
                    f"the sidecar did not finish task {task_id} within {self.poll_timeout:.0f}s"
                )
            try:
                status = requests.get(
                    f"{self.base_url}/v1/status/poll/{task_id}", timeout=self.timeout
                )
                status.raise_for_status()
                state = str(status.json().get("task_status", ""))
            except requests.RequestException as exc:
                raise DocumentParserUnavailableError(
                    "lost contact with the document parser sidecar mid-conversion",
                    detail=str(exc),
                ) from exc

            if state in _SUCCESS_STATES:
                break
            if state in _FAILURE_STATES:
                raise DocumentParseError(f"the sidecar failed to convert this document ({state})")
            time.sleep(_POLL_INTERVAL_SECONDS)

        try:
            result = requests.get(f"{self.base_url}/v1/result/{task_id}", timeout=self.timeout)
            result.raise_for_status()
            return dict(result.json())
        except requests.RequestException as exc:
            raise DocumentParserUnavailableError(
                "the sidecar finished but its result could not be fetched", detail=str(exc)
            ) from exc

    # -- response → IR -------------------------------------------------------------

    def _to_ir(
        self, result: dict[str, Any], source: ParseSource, options: ParseOptions
    ) -> ParsedDocument:
        """Rebuild a ``DoclingDocument`` from the response and walk it into the IR.

        The walk is shared with the slim tier (``docling_slim._blocks_from_docling``) on
        purpose: two tiers producing structurally different IR for the same document would
        make ``parser`` a hidden variable in every retrieval measurement.
        """
        from docling_core.types.doc import DoclingDocument

        from .docling_slim import _blocks_from_docling

        document = result.get("document") or {}
        json_content = document.get("json_content")
        if not json_content:
            raise DocumentEmptyError("the sidecar returned no document content")

        doc = DoclingDocument.model_validate(json_content)
        builder = IRBuilder()
        pages_with_text = _blocks_from_docling(doc, builder)

        pages = getattr(doc, "pages", None) or {}
        page_count = len(pages)
        ocr_pages, warnings = _ocr_coverage(pages_with_text, page_count, options)

        if len(builder) == 0:
            raise DocumentEmptyError("the sidecar produced no text from this document")

        return builder.build(
            parser=self.name,
            parser_version=self.version,
            page_count=page_count,
            language=options.language,
            has_embedded_text=True,
            ocr_applied=options.ocr in ("auto", "force"),
            ocr_pages=ocr_pages,
            metadata={"mime": source.mime, "tier": "serve"},
            warnings=warnings,
        )


def _ocr_coverage(
    pages_with_text: set[int], page_count: int, options: ParseOptions
) -> tuple[int, list[str]]:
    """Count pages that produced text, and **name the shortfall as a warning**.

    This is the "silent OCR degradation must surface in the parse notification" rule.
    OCR that yields nothing for 40 of 200 pages currently looks identical to a document
    that was simply short; the only place that difference is knowable is right here,
    while the per-page provenance still exists.

    The page set comes from the same walk that built the blocks rather than a second pass:
    a coverage check that re-derives pages independently is a coverage check that can
    disagree with the IR it is describing.
    """
    warnings: list[str] = []
    if options.ocr == "never":
        return 0, warnings

    covered = len(pages_with_text)
    if page_count and covered < page_count:
        warnings.append(
            f"OCR produced no text for {page_count - covered} of {page_count} pages — "
            f"those pages are not searchable"
        )
    return covered, warnings
